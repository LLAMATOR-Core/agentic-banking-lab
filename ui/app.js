const config = {
  authority: "http://localhost:8080/realms/agentic-banking",
  clientId: "bank-ui",
  redirectUri: "http://localhost:3000/",
  api: "http://localhost:8000",
};

const state = {
  tokens: JSON.parse(sessionStorage.getItem("bank.tokens") || "null"),
  conversationId: sessionStorage.getItem("bank.conversation") || crypto.randomUUID(),
  pendingConfirmation: null,
  dashboard: null,
};
sessionStorage.setItem("bank.conversation", state.conversationId);

const $ = (id) => document.getElementById(id);
const create = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

async function sha256(text) {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text)));
}
function base64url(bytes) {
  return btoa(String.fromCharCode(...bytes)).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
}
function randomValue() { return base64url(crypto.getRandomValues(new Uint8Array(32))); }

async function login() {
  const verifier = randomValue();
  const oauthState = randomValue();
  sessionStorage.setItem("oauth.verifier", verifier);
  sessionStorage.setItem("oauth.state", oauthState);
  const challenge = base64url(await sha256(verifier));
  const url = new URL(`${config.authority}/protocol/openid-connect/auth`);
  url.search = new URLSearchParams({
    client_id: config.clientId, redirect_uri: config.redirectUri, response_type: "code",
    scope: "openid profile email", state: oauthState, code_challenge: challenge,
    code_challenge_method: "S256",
  });
  location.assign(url);
}

async function handleCallback() {
  const params = new URLSearchParams(location.search);
  if (!params.has("code")) return;
  if (params.get("state") !== sessionStorage.getItem("oauth.state")) throw new Error("OAuth state mismatch");
  const body = new URLSearchParams({
    grant_type: "authorization_code", client_id: config.clientId, code: params.get("code"),
    redirect_uri: config.redirectUri, code_verifier: sessionStorage.getItem("oauth.verifier"),
  });
  const response = await fetch(`${config.authority}/protocol/openid-connect/token`, { method: "POST", headers: {"Content-Type": "application/x-www-form-urlencoded"}, body });
  if (!response.ok) throw new Error("Не удалось обменять OAuth-код");
  const tokens = await response.json();
  tokens.expires_at = Date.now() + tokens.expires_in * 1000;
  state.tokens = tokens;
  sessionStorage.setItem("bank.tokens", JSON.stringify(tokens));
  history.replaceState({}, "", "/");
}

async function refreshToken() {
  if (!state.tokens?.refresh_token) return false;
  const body = new URLSearchParams({ grant_type: "refresh_token", client_id: config.clientId, refresh_token: state.tokens.refresh_token });
  const response = await fetch(`${config.authority}/protocol/openid-connect/token`, { method: "POST", headers: {"Content-Type": "application/x-www-form-urlencoded"}, body });
  if (!response.ok) return false;
  const tokens = await response.json();
  tokens.expires_at = Date.now() + tokens.expires_in * 1000;
  state.tokens = tokens;
  sessionStorage.setItem("bank.tokens", JSON.stringify(tokens));
  return true;
}

async function accessToken() {
  if (!state.tokens) throw new Error("Войдите в систему");
  if (Date.now() > state.tokens.expires_at - 30000 && !(await refreshToken())) {
    sessionStorage.removeItem("bank.tokens"); location.reload();
  }
  return state.tokens.access_token;
}

async function api(path, options = {}) {
  const token = await accessToken();
  const response = await fetch(`${config.api}${path}`, {
    ...options,
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${response.status}`);
  }
  return response.json();
}

async function loadDashboard() {
  const data = await api("/v1/dashboard");
  state.dashboard = data;
  const firstName = data.customer.full_name.split(" ")[1] || data.customer.full_name;
  $("greeting").textContent = `Добрый день, ${firstName}`;
  $("user-name").textContent = data.customer.full_name;
  $("user-tier").textContent = data.customer.vip ? "VIP-клиент" : "Клиент";
  $("user-initials").textContent = data.customer.full_name.split(" ").slice(0, 2).map((v) => v[0]).join("");
  const rub = data.accounts.filter((account) => account.currency === "RUB").reduce((sum, account) => sum + Number(account.balance), 0);
  $("total-balance").textContent = money(rub, "RUB");
  renderAccounts(data.accounts);
  renderOperations(data.operations);
  const portfolio = data.portfolio.reduce((sum, item) => sum + Number(item.market_value), 0);
  $("portfolio-total").textContent = money(portfolio, "RUB");
}

function money(value, currency) { return new Intl.NumberFormat("ru-RU", {style: "currency", currency, maximumFractionDigits: 2}).format(value); }
function renderAccounts(accounts) {
  $("account-cards").replaceChildren(...accounts.map((account) => {
    const card = create("div", "mini-account");
    card.append(create("small", "", `${account.kind} · ${account.masked_number}`), create("strong", "", money(Number(account.balance), account.currency)));
    return card;
  }));
  $("transfer-from").replaceChildren(...accounts.filter((a) => a.status === "active" && a.currency === "RUB").map((account) => {
    const option = create("option", "", `${account.masked_number} · ${account.balance} RUB`); option.value = account.id; return option;
  }));
}
function renderOperations(operations) {
  $("operations-list").replaceChildren(...operations.slice(0, 5).map((item) => {
    const row = create("div", "operation");
    const icon = create("div", "operation-icon", item.direction === "incoming" ? "↓" : "↑");
    const label = create("div"); label.append(create("strong", "", item.counterparty), create("small", "", `${item.category} · ${new Date(item.created_at).toLocaleDateString("ru-RU")}`));
    const sign = item.direction === "incoming" ? "+" : "−";
    row.append(icon, label, create("strong", `amount ${item.direction === "incoming" ? "in" : "out"}`, `${sign}${money(Number(item.amount), item.currency)}`));
    return row;
  }));
}

function addMessage(role, text) {
  const list = $("messages");
  list.append(create("div", `message ${role}`, text));
  list.scrollTop = list.scrollHeight;
}

async function sendChat(message, extra = {}) {
  addMessage("user", message);
  const typing = create("div", "message agent", "Секунду…"); $("messages").append(typing);
  try {
    const result = await api("/v1/chat", { method: "POST", body: JSON.stringify({ message, conversation_id: state.conversationId, ...extra }) });
    typing.remove();
    addMessage("agent", result.message);
    if (result.requires_confirmation) {
      state.pendingConfirmation = result.confirmation_id;
      $("confirmation-box").classList.remove("hidden");
    } else {
      state.pendingConfirmation = null; $("confirmation-box").classList.add("hidden");
    }
    if (["accounts", "operations", "action"].includes(result.intent)) await loadDashboard();
  } catch (error) { typing.remove(); addMessage("agent", `Ошибка: ${error.message}`); }
}

function isoDate(offset) { const date = new Date(); date.setDate(date.getDate() + offset); return date.toISOString().slice(0, 10); }

async function init() {
  try { await handleCallback(); } catch (error) { alert(error.message); }
  if (!state.tokens) return;
  $("login-view").classList.add("hidden"); $("app-view").classList.remove("hidden");
  $("travel-from").value = isoDate(30); $("travel-to").value = isoDate(37);
  try { await loadDashboard(); } catch (error) { addMessage("agent", `Не удалось загрузить обзор: ${error.message}`); }
}

$("login-button").addEventListener("click", login);
$("logout-button").addEventListener("click", () => {
  const hint = state.tokens?.id_token; sessionStorage.clear();
  const url = new URL(`${config.authority}/protocol/openid-connect/logout`);
  url.search = new URLSearchParams({ id_token_hint: hint, post_logout_redirect_uri: config.redirectUri }); location.assign(url);
});
$("chat-form").addEventListener("submit", (event) => { event.preventDefault(); const input = $("chat-input"); if (input.value.trim()) sendChat(input.value.trim()); input.value = ""; });
document.querySelectorAll("[data-prompt]").forEach((button) => button.addEventListener("click", () => sendChat(button.dataset.prompt)));
$("prepare-transfer").addEventListener("click", () => sendChat("Подготовь перевод по указанным реквизитам", { action: { type: "transfer", parameters: { from_account: $("transfer-from").value, to_account: $("transfer-to").value, amount: $("transfer-amount").value, description: "Перевод из интернет-банка" } } }));
$("confirm-button").addEventListener("click", () => state.pendingConfirmation && sendChat("Подтверждаю подготовленную операцию", { confirm_action_id: state.pendingConfirmation }));
$("plan-travel").addEventListener("click", () => sendChat(`Спланируй поездку в ${$("travel-city").value}`, { travel: { origin: "MOW", destination: $("travel-city").value, destination_airport: $("travel-airport").value.toUpperCase(), departure_date: $("travel-from").value, return_date: $("travel-to").value, adults: 1, budget: Number($("travel-budget").value), currency: "RUB", interests: ["culture", "food"] } }));

init();
