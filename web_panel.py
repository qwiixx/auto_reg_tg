"""Local aiohttp panel for registration and downloading Telegram session files."""

import argparse
import os
import re
from pathlib import Path

from aiohttp import web

from number_provider import load_provider_factory
from panel_auth import COOKIE_NAME, MAX_CREDENTIAL_LENGTH, SESSION_TTL, PanelAuth
from panel_jobs import PanelJobs
from phone_numbers import normalize_phone, parse_phone_list
from proxy_config import DEFAULT_NUMBERS_PER_PROXY, parse_proxy, parse_proxy_rotation
from registration import RegistrationService
from telegram_gateway import SESSIONS_DIR

HOST = "127.0.0.1"
LOGIN_PATH = "/api/auth/login"
ASSETS = Path(__file__).with_name("panel")
SESSION_NAME = re.compile(r"reg_([0-9]{8,})\.session")
MAX_BATCH = 200
MAX_AUTO_BATCH = 50
MAX_INPUT_LENGTH = 512
MAX_REQUEST_SIZE = 256 * 1024


def error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message}, status=status)


async def read_object(request: web.Request) -> dict:
    if request.content_type != "application/json":
        raise ValueError("Ожидается JSON.")
    try:
        value = await request.json()
    except (LookupError, RecursionError, ValueError) as exc:
        raise ValueError("Некорректный JSON.") from exc
    if not isinstance(value, dict):
        raise ValueError("Ожидается JSON-объект.")
    return value


class SessionFiles:
    def __init__(self, directory: Path) -> None:
        self.directory = directory.resolve()

    def find(self, name: str) -> Path | None:
        if SESSION_NAME.fullmatch(name) is None:
            return None
        path = self.directory / name
        if path.is_symlink() or not path.is_file() or path.resolve().parent != self.directory:
            return None
        return path

    def list(self, service: RegistrationService) -> list[dict]:
        files = []
        for candidate in self.directory.glob("reg_*.session"):
            match = SESSION_NAME.fullmatch(candidate.name)
            if match is None or self.find(candidate.name) is None:
                continue
            try:
                info = candidate.stat()
            except OSError:
                continue
            phone = "+" + match[1]
            files.append(
                {
                    "phone": phone,
                    "name": candidate.name,
                    "modified": int(info.st_mtime),
                    "available": not service.is_active(phone),
                }
            )
        return sorted(files, key=lambda file: (-file["modified"], file["name"]))


def create_app(
    jobs: PanelJobs, *, auth: PanelAuth, sessions_dir: Path = Path(SESSIONS_DIR)
) -> web.Application:
    sessions = SessionFiles(sessions_dir)

    @web.middleware
    async def protect(request, handler):
        if request.host.partition(":")[0] not in {HOST, "localhost"}:
            return error("Панель доступна по локальному адресу запуска.", 403)
        if request.path.startswith("/api/"):
            origin = request.headers.get("Origin")
            unsafe = request.method not in {"GET", "HEAD", "OPTIONS"}
            if (unsafe or origin is not None) and origin != f"{request.scheme}://{request.host}":
                return error("Запрос с другого адреса отклонён.", 403)
            if request.path != LOGIN_PATH and not auth.authorized(
                request.cookies.get(COOKIE_NAME, "")
            ):
                response = error("Войдите в панель.", 401)
                response.headers["Cache-Control"] = "no-store"
                return response
        try:
            response = await handler(request)
        except web.HTTPRequestEntityTooLarge:
            response = error(f"Тело запроса превышает {MAX_REQUEST_SIZE // 1024} КБ.", 413)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
                ),
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            }
        )
        return response

    async def login(request: web.Request) -> web.Response:
        try:
            data = await read_object(request)
        except ValueError as exc:
            return error(str(exc))
        username, password = data.get("username"), data.get("password")
        if not all(
            isinstance(value, str) and 0 < len(value) <= MAX_CREDENTIAL_LENGTH
            for value in (username, password)
        ):
            return error("Введите логин и пароль.")
        token, retry_after = auth.login(username, password)
        if retry_after:
            response = error(f"Слишком много попыток. Повторите через {retry_after} сек.", 429)
            response.headers["Retry-After"] = str(retry_after)
            return response
        if token is None:
            return error("Неверный логин или пароль.", 401)

        auth.logout(request.cookies.get(COOKIE_NAME, ""))
        response = web.Response(status=204)
        response.set_cookie(
            COOKIE_NAME,
            token,
            httponly=True,
            secure=request.secure,
            samesite="Strict",
            max_age=SESSION_TTL,
            path="/",
        )
        return response

    async def logout(request: web.Request) -> web.Response:
        auth.logout(request.cookies.get(COOKIE_NAME, ""))
        response = web.Response(status=204)
        response.del_cookie(COOKIE_NAME, path="/")
        return response

    async def state(request: web.Request) -> web.Response:
        return web.json_response({**jobs.snapshot(), "sessions": sessions.list(jobs.service)})

    async def start(request: web.Request) -> web.Response:
        if jobs.running:
            return error("Очередь уже выполняется.", 409)
        try:
            data = await read_object(request)
            mode = data.get("mode")
            rotation = (
                parse_proxy_rotation(
                    data["proxies"], data.get("numbers_per_proxy", DEFAULT_NUMBERS_PER_PROXY)
                )
                if "proxies" in data
                else None
            )
            if mode == "auto":
                if jobs.provider_factory is None:
                    return error("Укажите PANEL_PROVIDER или --provider при запуске панели.", 409)
                count = data.get("count")
                first_name = data.get("first_name")
                if type(count) is not int or not 1 <= count <= MAX_AUTO_BATCH:
                    raise ValueError(f"Укажите от 1 до {MAX_AUTO_BATCH} попыток.")
                if not isinstance(first_name, str) or not 1 <= len(first_name.strip()) <= 64:
                    raise ValueError("Введите имя длиной от 1 до 64 символов.")
                jobs.start([], count=count, first_name=first_name.strip(), rotation=rotation)
            elif mode == "phone" and isinstance(data.get("phone"), str):
                jobs.start([normalize_phone(data["phone"])], rotation=rotation)
            elif mode == "file" and isinstance(data.get("numbers"), str):
                phones = parse_phone_list(data["numbers"])
                if len(phones) > MAX_BATCH:
                    raise ValueError(f"Загрузите не более {MAX_BATCH} номеров за один раз.")
                jobs.start(phones, rotation=rotation)
            else:
                raise ValueError("Выберите режим и укажите данные для запуска.")
        except ValueError as exc:
            return error(str(exc))
        except RuntimeError as exc:
            return error(str(exc), 409)
        return web.json_response(jobs.snapshot(), status=202)

    async def stop(request: web.Request) -> web.Response:
        jobs.cancel()
        return web.json_response(jobs.snapshot())

    async def answer(request: web.Request) -> web.Response:
        try:
            data = await read_object(request)
        except ValueError as exc:
            return error(str(exc))
        value = data.get("value")
        if "value" not in data or (
            value is not None and (not isinstance(value, str) or len(value) > MAX_INPUT_LENGTH)
        ):
            return error("Некорректный ответ.")
        if not jobs.answer(request.match_info["id"], value):
            return error("Этот запрос уже закрыт.", 409)
        return web.json_response({"accepted": True})

    async def download(request: web.Request) -> web.Response:
        digits = request.match_info["digits"]
        if jobs.service.is_active("+" + digits):
            return error("Дождитесь завершения обработки номера.", 409)
        path = sessions.find(f"reg_{digits}.session")
        if path is None:
            return error("Файл сессии не найден.", 404)
        try:
            # Read a snapshot before yielding to any task that could reopen this session.
            content = path.read_bytes()
        except OSError:
            return error("Не удалось прочитать файл сессии.", 409)
        return web.Response(
            body=content,
            content_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{path.name}"'},
        )

    async def close(app: web.Application) -> None:
        await jobs.close()

    async def page(request: web.Request) -> web.FileResponse:
        return web.FileResponse(
            ASSETS / "index.html", headers={"Content-Type": "text/html; charset=utf-8"}
        )

    async def stylesheet(request: web.Request) -> web.FileResponse:
        return web.FileResponse(
            ASSETS / "panel.css", headers={"Content-Type": "text/css; charset=utf-8"}
        )

    async def script(request: web.Request) -> web.FileResponse:
        return web.FileResponse(
            ASSETS / "panel.js", headers={"Content-Type": "text/javascript; charset=utf-8"}
        )

    app = web.Application(middlewares=[protect], client_max_size=MAX_REQUEST_SIZE)
    app.add_routes(
        [
            web.get("/", page),
            web.get("/panel.css", stylesheet),
            web.get("/panel.js", script),
            web.post(LOGIN_PATH, login),
            web.post("/api/auth/logout", logout),
            web.get("/api/state", state),
            web.post("/api/jobs", start),
            web.delete("/api/jobs/current", stop),
            web.post("/api/prompts/{id}", answer),
            web.get(r"/api/sessions/{digits:[0-9]{8,}}", download, allow_head=False),
        ]
    )
    app.on_shutdown.append(close)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Локальная веб-панель Telegram.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--listen", choices=(HOST, "0.0.0.0"), default=HOST)
    parser.add_argument(
        "--provider", default=os.getenv("PANEL_PROVIDER"), help="Фабрика в формате module:factory"
    )
    parser.add_argument(
        "--proxy",
        default=os.getenv("PANEL_PROXY"),
        help="HTTP/SOCKS5 прокси для Telegram и Mail.tm",
    )
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port должен быть от 1 до 65535.")
    try:
        proxy = parse_proxy(args.proxy) if args.proxy else None
        factory = load_provider_factory(args.provider) if args.provider else None
        password_file = os.getenv("PANEL_PASSWORD_FILE")
        password = (
            Path(password_file).read_text(encoding="utf-8-sig").rstrip("\r\n")
            if password_file
            else os.getenv("PANEL_PASSWORD", "")
        )
        auth = PanelAuth(os.getenv("PANEL_USERNAME", "admin"), password)
    except OSError:
        parser.error("Не удалось прочитать файл PANEL_PASSWORD_FILE.")
    except ValueError as exc:
        parser.error(str(exc))
    service = RegistrationService(os.getenv("DEVICE_TOKEN", "").strip())
    jobs = PanelJobs(service, factory, proxy=proxy, mail_proxy_url=args.proxy)
    web.run_app(
        create_app(jobs, auth=auth),
        host=args.listen,
        port=args.port,
        access_log=None,
        print=lambda _: print(f"Панель запущена на порту {args.port}.", flush=True),
    )


if __name__ == "__main__":
    main()
