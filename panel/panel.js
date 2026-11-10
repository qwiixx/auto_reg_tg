"use strict";

const element = (id) => document.getElementById(id);
const labels = {
  on_disk: "На диске",
  queued: "В очереди",
  running: "Обработка",
  waiting: "Ждёт ввода",
  succeeded: "Готово",
  already_authorized: "Уже авторизован",
  failed: "Ошибка",
  cancelled: "Остановлено",
};
const reasons = {
  already_running: "Номер уже обрабатывается.",
  app_code_without_fallback: "Telegram предлагает код только в приложении.",
  code_expired: "Срок действия кода истёк.",
  code_invalid: "Telegram отклонил код.",
  code_missing: "Код пропущен или не получен.",
  first_name_required: "Требуется имя для нового аккаунта.",
  invalid_phone: "Некорректный номер.",
  mail_error: "Ошибка Mail.tm.",
  mail_timeout: "Письмо не пришло вовремя.",
  panel_input_timeout: "Время на ввод ответа истекло.",
  password_required: "Требуется пароль 2FA.",
  payment_required: "Telegram запросил платную верификацию.",
  provider_acquire_error: "Провайдер не выдал номер.",
  provider_acquire_timeout: "Номер не получен вовремя.",
  provider_code_error: "Провайдер вернул ошибку получения кода.",
  provider_code_timeout: "Код не получен вовремя.",
  provider_delivery_unsupported: "Этот канал требует ручного ввода.",
  registration_error: "Ошибка регистрации.",
  telegram_error: "Telegram вернул ошибку.",
  telegram_rate_limited: "Telegram ограничил частоту запросов.",
  unexpected_email_response: "Неожиданный ответ при подтверждении почты.",
};
const promptLabels = {
  code: "Код Telegram",
  password: "Пароль 2FA",
  first_name: "Имя нового аккаунта",
};
const loginForm = element("login-form");
const launch = element("launch");
const promptForm = element("prompt-form");
const promptInput = element("answer");
let state = null;
let online = false;
let needsLogin = false;
let launching = false;
let answering = false;
let stopping = false;
let currentPrompt = null;
let refreshVersion = 0;
let renderedTable = "";

function message(id, text) {
  const node = element(id);
  node.hidden = !text;
  if (node.textContent !== text) node.textContent = text;
}

async function api(path, options = {}) {
  const headers = options.body ? { "Content-Type": "application/json" } : {};
  const response = await fetch(path, {
    ...options, headers, cache: "no-store", credentials: "same-origin",
  });
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    const error = new Error(data?.error || `Ошибка запроса (${response.status}).`);
    error.status = response.status;
    throw error;
  }
  return response;
}

function setConnection(status, label) {
  const connection = element("connection");
  connection.dataset.state = status;
  connection.lastElementChild.textContent = label;
}

function showLogin() {
  const hadSession = state !== null;
  refreshVersion += 1;
  needsLogin = true;
  state = null;
  online = false;
  renderedTable = "";
  element("rows").replaceChildren();
  element("search").value = "";
  element("dashboard").hidden = true;
  element("logout").hidden = true;
  element("signin").hidden = false;
  launch.reset();
  renderPrompt();
  updateControls();
  setConnection("offline", "Вход");
  if (hadSession) element("username").focus();
}

function handleError(error, target) {
  if (error.status === 401) {
    showLogin();
  } else {
    message(target, error.message);
  }
}

function updateControls() {
  const mode = launch.elements.mode.value;
  for (const item of ["phone", "file", "auto"]) {
    element(`mode-${item}`).hidden = item !== mode;
  }
  const providerMissing = state && !state.auto_available;
  element("provider-note").hidden = !providerMissing;
  element("start").disabled = !online || launching || state?.running ||
    (mode === "auto" && providerMissing);
  const stop = element("stop");
  stop.hidden = !state?.running;
  stop.disabled = !online || stopping || state?.stopping;
  stop.textContent = state?.stopping ? "Останавливаем…" : "Остановить очередь";
  for (const button of promptForm.querySelectorAll("button")) {
    button.disabled = !online || answering || state?.stopping;
  }
}

function renderPrompt() {
  const prompt = state?.prompt;
  if (currentPrompt === (prompt?.id ?? null)) return;
  currentPrompt = prompt?.id ?? null;
  element("challenge").hidden = !prompt;
  promptInput.value = "";
  message("prompt-error", "");
  if (!prompt) return;

  const label = promptLabels[prompt.kind];
  element("challenge-title").textContent = label;
  element("answer-label").textContent = label;
  const details = [prompt.phone];
  if (prompt.challenge?.email_pattern) details.push(prompt.challenge.email_pattern);
  if (prompt.challenge?.length) details.push(`${prompt.challenge.length} симв.`);
  if (prompt.challenge) {
    details.push(`попытка ${prompt.challenge.attempt} из ${prompt.challenge.max_attempts}`);
  }
  element("challenge-detail").textContent = details.join(" · ");
  promptInput.type = prompt.kind === "password" ? "password" : "text";
  promptInput.autocomplete = prompt.kind === "code" ? "one-time-code" : "off";
  promptInput.inputMode = "text";
  if (prompt.challenge && /^SentCodeType(?:Sms|FirebaseSms|EmailCode)$/.test(prompt.challenge.delivery)) {
    promptInput.inputMode = "numeric";
  }
  promptInput.focus();
}

function addDetail(cell, text) {
  const detail = document.createElement("span");
  detail.className = "detail";
  detail.textContent = text;
  cell.append(detail);
}

async function downloadSession(file, button) {
  button.disabled = true;
  try {
    const response = await api(`/api/sessions/${encodeURIComponent(file.phone.slice(1))}`);
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = file.name;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30_000);
    message("notice", "");
  } catch (error) {
    handleError(error, "notice");
  } finally {
    button.disabled = false;
  }
}

function renderFile(cell, file) {
  if (!file) {
    cell.textContent = "—";
    return;
  }
  if (file.available) {
    const button = document.createElement("button");
    button.className = "download";
    button.type = "button";
    button.textContent = "↓ Скачать";
    button.setAttribute("aria-label", `Скачать ${file.name}`);
    button.addEventListener("click", () => downloadSession(file, button));
    cell.append(button);
  }
  addDetail(cell, file.name);
}

function renderTable() {
  if (!state) return;
  const query = element("search").value.replace(/\D/g, "");
  const files = new Map(state.sessions.map((file) => [file.phone, file]));
  const queuedPhones = new Set(state.rows.map((row) => row.phone).filter(Boolean));
  const rows = state.rows.concat(state.sessions
    .filter((file) => !queuedPhones.has(file.phone))
    .map((file) => ({ phone: file.phone, status: "on_disk" })));
  const visible = rows.filter((row) => !query || row.phone?.includes(query));
  element("metric-phones").textContent = new Set(rows.map((row) => row.phone).filter(Boolean)).size;
  element("metric-files").textContent = files.size;
  element("metric-queue").textContent = state.rows.filter(
    (row) => row.status === "queued" || row.status === "running",
  ).length;
  const signature = JSON.stringify([visible, state.sessions, state.prompt?.phone, query]);
  if (renderedTable === signature) return;
  renderedTable = signature;
  const body = document.createDocumentFragment();
  for (const row of visible) {
    const tr = document.createElement("tr");
    const phone = tr.insertCell();
    phone.textContent = row.phone || "Ожидается номер";
    const status = tr.insertCell();
    const badge = document.createElement("span");
    const waiting = row.status === "running" && state.prompt?.phone === row.phone;
    badge.className = "status";
    badge.dataset.status = row.status;
    badge.textContent = labels[waiting ? "waiting" : row.status] || row.status;
    status.append(badge);
    if (row.reason) addDetail(status, reasons[row.reason] || "Вход не завершён.");
    if (row.provider_cleanup_failed) addDetail(status, "Провайдер не подтвердил завершение.");
    renderFile(tr.insertCell(), files.get(row.phone));
    body.append(tr);
  }
  element("rows").replaceChildren(body);
  element("empty").hidden = visible.length !== 0;
  element("empty-title").textContent = query ? "Совпадений нет" : "Список пока пуст";
  element("empty-text").textContent = query
    ? "Попробуйте другую часть номера."
    : "Добавьте номер или загрузите список, чтобы начать.";
}

async function refresh() {
  const version = ++refreshVersion;
  try {
    const response = await api("/api/state");
    const next = await response.json();
    if (version !== refreshVersion) return;
    if (!online) message("notice", "");
    state = next;
    online = true;
    needsLogin = false;
    element("signin").hidden = true;
    element("dashboard").hidden = false;
    element("logout").hidden = false;
    if (state.error) message("notice", state.error);
  } catch (error) {
    if (version !== refreshVersion) return;
    if (error.status === 401) {
      showLogin();
      return;
    }
    online = false;
    message(state ? "notice" : "login-error", error.status
      ? error.message : "Связь с панелью потеряна. Проверяем подключение.");
  }
  setConnection(online ? "online" : "offline", online ? "Локальный доступ" : "Нет подключения");
  updateControls();
  renderPrompt();
  renderTable();
}

async function readTextFile(file, maxKiB) {
  if (file.size > maxKiB * 1024) {
    throw new Error(`Выберите файл размером до ${maxKiB} КБ.`);
  }
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
  } catch {
    throw new Error("Не удалось прочитать файл в UTF‑8.");
  }
}

async function launchBatch(event) {
  event.preventDefault();
  if (launching) return;
  message("launch-error", "");
  launching = true;
  updateControls();
  try {
    const mode = launch.elements.mode.value;
    const proxyFile = element("proxy-file").files[0];
    const numbersPerProxy = Number(element("numbers-per-proxy").value);
    const data = { mode };
    if (mode === "phone") {
      data.phone = element("phone").value;
    } else if (mode === "file") {
      const file = element("file").files[0];
      if (!file) throw new Error("Выберите файл с номерами.");
      data.numbers = await readTextFile(file, 128);
    } else {
      data.count = element("count").valueAsNumber;
      data.first_name = element("first-name").value;
    }
    if (proxyFile) {
      data.proxies = await readTextFile(proxyFile, 64);
      data.numbers_per_proxy = numbersPerProxy;
    }
    await api("/api/jobs", { method: "POST", body: JSON.stringify(data) });
    await refresh();
  } catch (error) {
    handleError(error, "launch-error");
  } finally {
    launching = false;
    updateControls();
  }
}

async function answerPrompt(value) {
  const id = currentPrompt;
  if (!id || answering) return;
  if (value === "") {
    message("prompt-error", "Введите ответ или нажмите «Пропустить».");
    return;
  }
  answering = true;
  updateControls();
  try {
    await api(`/api/prompts/${encodeURIComponent(id)}`, {
      method: "POST",
      body: JSON.stringify({ value }),
    });
    if (currentPrompt === id) promptInput.value = "";
    await refresh();
  } catch (error) {
    if (currentPrompt === id) handleError(error, "prompt-error");
  } finally {
    answering = false;
    updateControls();
  }
}

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = loginForm.querySelector("button");
  const password = element("password");
  button.disabled = true;
  message("login-error", "");
  try {
    await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username: element("username").value, password: password.value }),
    });
    await refresh();
  } catch (error) {
    message("login-error", error.message);
  } finally {
    password.value = "";
    button.disabled = false;
  }
});
element("logout").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    await api("/api/auth/logout", { method: "POST" });
    message("login-error", "");
    showLogin();
  } catch (error) {
    handleError(error, "notice");
  } finally {
    button.disabled = false;
  }
});
launch.addEventListener("submit", launchBatch);
launch.addEventListener("change", updateControls);
promptForm.addEventListener("submit", (event) => {
  event.preventDefault();
  answerPrompt(promptInput.value);
});
element("skip").addEventListener("click", () => answerPrompt(null));
element("search").addEventListener("input", renderTable);
element("stop").addEventListener("click", async () => {
  stopping = true;
  updateControls();
  try {
    await api("/api/jobs/current", { method: "DELETE" });
    await refresh();
  } catch (error) {
    handleError(error, "launch-error");
  } finally {
    stopping = false;
    updateControls();
  }
});

async function poll() {
  if (!needsLogin) await refresh();
  setTimeout(poll, state?.running ? 900 : 2500);
}

poll();
