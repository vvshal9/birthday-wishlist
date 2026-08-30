const CLAIMS_KEY = "bday-wishlist-claims-v1";
const MANAGE_KEY = "bday-wishlist-manage-v1";
const CONFIRM_MS = 8000;
const UNDO_MS = 12000;

const PLACEHOLDER_SVG = `<svg viewBox="0 0 64 64" fill="none" aria-hidden="true">
  <rect x="12" y="24" width="40" height="28" rx="3" stroke="currentColor" stroke-width="2"/>
  <path d="M12 32h40" stroke="currentColor" stroke-width="2"/>
  <path d="M32 24v28" stroke="currentColor" stroke-width="2"/>
  <path d="M24 24c0-6 8-10 8-10s8 4 8 10" stroke="currentColor" stroke-width="2"/>
</svg>`;

const state = {
  items: [],
  filter: "all",
  category: "all",
  isHost: false,
  manageCode: sessionStorage.getItem(MANAGE_KEY) || "",
  pending: null,
  previewImage: "",
  toast: null,
  editingId: null,
  openId: null,
};

const els = {
  title: document.getElementById("page-title"),
  note: document.getElementById("page-note"),
  eyebrow: document.getElementById("eyebrow"),
  toolbar: document.getElementById("toolbar"),
  grid: document.getElementById("grid"),
  empty: document.getElementById("empty"),
  status: document.getElementById("status-line"),
  addSheet: document.getElementById("add-sheet"),
  codeSheet: document.getElementById("code-sheet"),
  cardSheet: document.getElementById("card-sheet"),
  cardViewer: document.getElementById("card-viewer"),
  toast: document.getElementById("toast"),
  toastText: document.getElementById("toast-text"),
  manageToggle: document.getElementById("manage-toggle"),
  addForm: document.getElementById("add-form"),
  addTitle: document.getElementById("add-title"),
  submitAdd: document.getElementById("submit-add"),
  url: document.getElementById("field-url"),
  titleField: document.getElementById("field-title"),
  priceField: document.getElementById("field-price"),
  commentField: document.getElementById("field-comment"),
  categoryField: document.getElementById("field-category"),
  imageUrlField: document.getElementById("field-image-url"),
  previewStatus: document.getElementById("preview-status"),
  imageRow: document.getElementById("image-row"),
  imagePreview: document.getElementById("image-preview"),
  noImage: document.getElementById("field-no-image"),
  codeForm: document.getElementById("code-form"),
  codeField: document.getElementById("field-code"),
  codeError: document.getElementById("code-error"),
};

let previewTimer = 0;
let pendingTimer = 0;
let toastTimer = 0;

function claims() {
  try {
    return JSON.parse(localStorage.getItem(CLAIMS_KEY) || "{}");
  } catch {
    return {};
  }
}

function saveClaim(id, token) {
  const next = claims();
  next[id] = token;
  localStorage.setItem(CLAIMS_KEY, JSON.stringify(next));
}

function dropClaim(id) {
  const next = claims();
  delete next[id];
  localStorage.setItem(CLAIMS_KEY, JSON.stringify(next));
}

function headers(extra = {}) {
  const h = { "Content-Type": "application/json", ...extra };
  if (state.manageCode) h["X-Manage-Code"] = state.manageCode;
  return h;
}

async function api(path, options = {}) {
  const res = await fetch(path, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "Не получилось");
  return data;
}

function formatPrice(item) {
  if (item.price == null || item.price === "") return null;
  try {
    return new Intl.NumberFormat("ru-RU", {
      style: "currency",
      currency: item.currency || "RUB",
      maximumFractionDigits: 0,
    }).format(item.price);
  } catch {
    return `${item.price} ${item.currency || ""}`.trim();
  }
}

function shopHost(url) {
  try {
    return new URL(url).host.replace(/^www\./, "");
  } catch {
    return "магазин";
  }
}

function imageSrc(url) {
  return `/api/image?url=${encodeURIComponent(url)}`;
}

function escapeAttr(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;")
    .replace(/</g, "&lt;");
}

function bindImageFallback(root) {
  root.querySelectorAll("img[data-direct]").forEach((img) => {
    img.addEventListener("error", () => {
      if (img.dataset.failed) return;
      img.dataset.failed = "1";
      img.src = imageSrc(img.dataset.direct);
    });
  });
}

function showImagePreview(url) {
  state.previewImage = url || "";
  if (url) {
    els.imageRow.hidden = false;
    els.imagePreview.referrerPolicy = "no-referrer";
    els.imagePreview.onerror = () => {
      els.imagePreview.onerror = null;
      els.imagePreview.src = imageSrc(url);
    };
    els.imagePreview.src = url;
    els.noImage.checked = false;
  } else {
    els.imageRow.hidden = true;
  }
}

function setHostMode(on, code = "") {
  state.isHost = on;
  state.manageCode = on ? code : "";
  if (on) sessionStorage.setItem(MANAGE_KEY, code);
  else sessionStorage.removeItem(MANAGE_KEY);
  els.toolbar.hidden = !on;
  els.manageToggle.textContent = on
    ? "Выйти из режима составления списка"
    : "Я составляю этот список";
}

function showSheet(el, show) {
  el.hidden = !show;
  syncLock();
}

function syncLock() {
  const open = [els.cardSheet, els.addSheet, els.codeSheet].some((node) => node && !node.hidden);
  document.body.classList.toggle("is-locked", open);
}

function clearPending() {
  state.pending = null;
  clearTimeout(pendingTimer);
}

function armPending(id, action) {
  clearPending();
  state.pending = { id, action };
  pendingTimer = setTimeout(() => {
    clearPending();
    render();
  }, CONFIRM_MS);
  render();
}

function hideToast() {
  state.toast = null;
  els.toast.hidden = true;
  clearTimeout(toastTimer);
}

function showUndoToast(item) {
  hideToast();
  state.toast = { id: item.id };
  els.toastText.textContent = "Отмечено. Если нажал случайно — отмени выбор.";
  els.toast.hidden = false;
  toastTimer = setTimeout(hideToast, UNDO_MS);
}

const CATEGORY_LABELS = {
  certificates: "Сертификаты",
  books: "Книги",
  things: "Вещи",
};

const CATEGORY_BADGE = {
  certificates: "Сертификат",
  books: "Книга",
  things: "Вещь",
};

function guessCategory(title, url) {
  const text = `${title || ""} ${url || ""}`.toLowerCase();
  if (
    [
      "сертификат",
      "certificate",
      "gift_certificate",
      "cuva.ru",
      "aerograd",
      "promocards",
      "прыжок",
    ].some((token) => text.includes(token))
  ) {
    return "certificates";
  }
  if (["chitai-gorod", "книга", "litres", "book24", "буквоед"].some((token) => text.includes(token))) {
    return "books";
  }
  return "things";
}

function itemCategory(item) {
  const value = item && item.category;
  return CATEGORY_LABELS[value] ? value : guessCategory(item && item.title, item && item.url);
}

function emptyCopy() {
  const catLabel = CATEGORY_LABELS[state.category];
  if (state.filter === "reserved") {
    return catLabel
      ? `В категории «${catLabel}» пока никто ничего не забронировал.`
      : "Пока никто ничего не забронировал.";
  }
  if (state.filter === "available") {
    return catLabel ? `Свободных карточек в категории «${catLabel}» нет.` : "Свободных карточек нет.";
  }
  if (catLabel) return `В категории «${catLabel}» пока пусто.`;
  if (state.isHost) return "Список пуст. Добавь первую карточку — друзья увидят её по ссылке.";
  return "Подарков пока нет. Загляни позже.";
}

function matchesStatus(item) {
  return state.filter === "all" || item.status === state.filter;
}

function matchesCategory(item) {
  return state.category === "all" || itemCategory(item) === state.category;
}

function visibleItems() {
  return state.items.filter((item) => matchesStatus(item) && matchesCategory(item));
}

function actionsHtml(item) {
  const reserved = item.status === "reserved";
  const mine = Boolean(claims()[item.id]);
  const pending = state.pending && state.pending.id === item.id;

  if (state.isHost) {
    if (pending && state.pending.action === "delete") {
      return `
        <p class="confirm-note">Удалить карточку из списка?</p>
        <button class="btn btn-danger" data-act="confirm-delete" data-id="${item.id}">Удалить</button>
        <button class="btn btn-quiet" data-act="cancel-pending">Отмена</button>`;
    }
    return `
      <button class="btn btn-quiet" data-act="edit" data-id="${item.id}">Изменить</button>
      <button class="btn btn-quiet" data-act="arm-delete" data-id="${item.id}">Удалить</button>`;
  }
  if (pending && state.pending.action === "reserve") {
    return `
      <p class="confirm-note">Подтверди — или отмени, если нажал случайно.</p>
      <button class="btn btn-primary" data-act="confirm-reserve" data-id="${item.id}">Подарю</button>
      <button class="btn btn-quiet" data-act="cancel-pending">Отмена</button>`;
  }
  if (pending && state.pending.action === "unreserve") {
    return `
      <p class="confirm-note">Отменить выбор? Карточка снова станет свободной.</p>
      <button class="btn" data-act="confirm-unreserve" data-id="${item.id}">Отменить выбор</button>
      <button class="btn btn-quiet" data-act="cancel-pending">Отмена</button>`;
  }
  if (!reserved) {
    return `<button class="btn btn-primary" data-act="arm-reserve" data-id="${item.id}">Подарю</button>`;
  }
  if (mine) {
    return `<button class="btn btn-quiet" data-act="arm-unreserve" data-id="${item.id}">Отменить выбор</button>`;
  }
  return "";
}

function cardHtml(item, viewer = false) {
  const reserved = item.status === "reserved";
  const price = formatPrice(item);
  const media = item.imageUrl
    ? `<img src="${escapeAttr(item.imageUrl)}" data-direct="${escapeAttr(item.imageUrl)}" alt="" referrerpolicy="no-referrer">`
    : `<div class="ph">${PLACEHOLDER_SVG}</div>`;
  const closeBtn = viewer
    ? `<button type="button" class="icon-btn viewer-close" data-act="close-viewer" aria-label="Закрыть">×</button>`
    : "";

  return `
    <article class="card ${viewer ? "viewer" : ""} ${reserved ? "is-reserved" : ""}" data-id="${item.id}">
      ${closeBtn}
      <div class="media">
        ${media}
        ${reserved ? `<span class="stamp">занято</span>` : ""}
      </div>
      <div class="body">
        <div class="badges">
          <span class="badge ${reserved ? "badge-busy" : "badge-free"}">${reserved ? "Забронировано" : "Свободно"}</span>
          <span class="badge badge-cat">${CATEGORY_BADGE[itemCategory(item)] || "Вещь"}</span>
        </div>
        <h2 class="title" ${viewer ? 'id="viewer-title"' : ""}></h2>
        <p class="comment" hidden></p>
        <dl class="extras" hidden></dl>
        <p class="price ${price ? "" : "is-empty"}">${price || "цена не указана"}</p>
        <a class="shop" href="${item.url || "#"}" target="_blank" rel="noopener noreferrer">${shopHost(item.url)} ↗</a>
        <div class="actions">${actionsHtml(item)}</div>
      </div>
    </article>`;
}

const CORE_FIELDS = new Set([
  "id",
  "title",
  "url",
  "price",
  "currency",
  "imageUrl",
  "comment",
  "category",
  "status",
  "createdAt",
  "updatedAt",
]);

function extraEntries(item) {
  return Object.entries(item || {}).filter(([key, value]) => {
    if (CORE_FIELDS.has(key)) return false;
    if (value == null || value === "") return false;
    return typeof value === "string" || typeof value === "number" || typeof value === "boolean";
  });
}

function fillCardText(node, item) {
  const titleEl = node.querySelector(".title");
  if (titleEl) titleEl.textContent = item.title || "";
  const commentEl = node.querySelector(".comment");
  if (commentEl) {
    if (item.comment) {
      commentEl.hidden = false;
      commentEl.textContent = item.comment;
    } else {
      commentEl.hidden = true;
    }
  }
  const extrasEl = node.querySelector(".extras");
  if (extrasEl) {
    extrasEl.innerHTML = "";
    const extras = extraEntries(item);
    extrasEl.hidden = extras.length === 0;
    extras.forEach(([key, value]) => {
      const dt = document.createElement("dt");
      dt.textContent = key;
      const dd = document.createElement("dd");
      dd.textContent = String(value);
      extrasEl.append(dt, dd);
    });
  }
}

function openCard(id) {
  state.openId = id;
  renderViewer();
}

function closeCard() {
  state.openId = null;
  els.cardSheet.hidden = true;
  els.cardViewer.innerHTML = "";
  syncLock();
}

function renderViewer() {
  if (!state.openId) {
    if (!els.cardSheet.hidden) closeCard();
    return;
  }
  const item = state.items.find((entry) => entry.id === state.openId);
  if (!item) {
    closeCard();
    return;
  }
  els.cardViewer.innerHTML = cardHtml(item, true);
  fillCardText(els.cardViewer, item);
  bindImageFallback(els.cardViewer);
  els.cardSheet.hidden = false;
  syncLock();
}

let lastViewKey = "";

function viewKey() {
  return JSON.stringify({
    items: state.items,
    filter: state.filter,
    category: state.category,
    isHost: state.isHost,
    pending: state.pending,
    openId: state.openId,
  });
}

function render() {
  const key = viewKey();
  if (key === lastViewKey) return;
  lastViewKey = key;
  const inCategory = state.items.filter(matchesCategory);
  const reservedCount = inCategory.filter((item) => item.status === "reserved").length;
  document.querySelector('[data-count="all"]').textContent = String(inCategory.length);
  document.querySelector('[data-count="available"]').textContent = String(inCategory.length - reservedCount);
  document.querySelector('[data-count="reserved"]').textContent = String(reservedCount);

  const inStatus = state.items.filter(matchesStatus);
  document.querySelector('[data-count-cat="all"]').textContent = String(inStatus.length);
  document.querySelector('[data-count-cat="certificates"]').textContent = String(
    inStatus.filter((item) => itemCategory(item) === "certificates").length
  );
  document.querySelector('[data-count-cat="books"]').textContent = String(
    inStatus.filter((item) => itemCategory(item) === "books").length
  );
  document.querySelector('[data-count-cat="things"]').textContent = String(
    inStatus.filter((item) => itemCategory(item) === "things").length
  );

  const items = visibleItems();
  els.empty.hidden = items.length > 0;
  els.empty.textContent = emptyCopy();
  els.grid.innerHTML = items.map((item) => cardHtml(item)).join("");
  [...els.grid.querySelectorAll(".card")].forEach((node, i) => {
    fillCardText(node, items[i]);
    bindImageFallback(node);
  });
  renderViewer();
}

async function loadItems() {
  const data = await api("/api/items");
  state.items = data.items || [];
  render();
}

async function loadConfig() {
  const cfg = await api("/api/config");
  document.title = cfg.title;
  els.title.textContent = cfg.title;
  els.note.textContent = cfg.note || "";
  els.eyebrow.textContent = "Вишлист";
}

async function previewUrl() {
  const url = els.url.value.trim();
  if (!url) return;
  els.previewStatus.textContent = "Читаю страницу…";
  try {
    const data = await api("/api/preview", {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({ url }),
    });
    if (data.title && !els.titleField.value) els.titleField.value = data.title;
    if (!state.editingId) {
      els.categoryField.value = guessCategory(els.titleField.value, url);
    }
    if (data.price != null && !els.priceField.value) els.priceField.value = String(data.price);
    if (data.imageUrl) {
      els.imageUrlField.value = data.imageUrl;
      showImagePreview(data.imageUrl);
    } else if (!els.imageUrlField.value) {
      showImagePreview("");
    }
    const bits = [];
    bits.push(data.title ? "название нашёл" : "название не нашёл — впиши сам");
    bits.push(data.price != null ? "цену нашёл" : "цену не нашёл — впиши сам");
    bits.push(data.imageUrl ? "картинку нашёл" : "картинку не нашёл — вставь прямую ссылку на фото");
    els.previewStatus.textContent = bits.join(" · ");
  } catch (err) {
    if (!els.imageUrlField.value) showImagePreview("");
    els.previewStatus.textContent = err.message;
  }
}

async function reserve(id) {
  const data = await api(`/api/items/${id}/reserve`, {
    method: "POST",
    headers: headers(),
    body: "{}",
  });
  saveClaim(id, data.claimToken);
  const idx = state.items.findIndex((item) => item.id === id);
  if (idx >= 0) state.items[idx] = data.item;
  clearPending();
  render();
  showUndoToast(data.item);
}

async function unreserve(id) {
  const data = await api(`/api/items/${id}/unreserve`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({ claimToken: claims()[id] || "" }),
  });
  dropClaim(id);
  const idx = state.items.findIndex((item) => item.id === id);
  if (idx >= 0) state.items[idx] = data.item;
  clearPending();
  hideToast();
  render();
}

async function removeItem(id) {
  await api(`/api/items/${id}`, { method: "DELETE", headers: headers(), body: "{}" });
  state.items = state.items.filter((item) => item.id !== id);
  dropClaim(id);
  clearPending();
  if (state.openId === id) closeCard();
  render();
}

async function handleItemAction(act, id) {
  if (act === "close-viewer") {
    closeCard();
    return;
  }
  if (act === "arm-reserve") armPending(id, "reserve");
  else if (act === "arm-unreserve") armPending(id, "unreserve");
  else if (act === "arm-delete") armPending(id, "delete");
  else if (act === "edit") {
    const item = state.items.find((entry) => entry.id === id);
    closeCard();
    if (item) fillEditor(item);
  } else if (act === "cancel-pending") {
    clearPending();
    render();
  } else if (act === "confirm-reserve") await reserve(id);
  else if (act === "confirm-unreserve") await unreserve(id);
  else if (act === "confirm-delete") await removeItem(id);
}

els.grid.addEventListener("click", async (event) => {
  const btn = event.target.closest("[data-act]");
  if (btn) {
    event.preventDefault();
    try {
      await handleItemAction(btn.dataset.act, btn.dataset.id);
    } catch (err) {
      clearPending();
      els.status.hidden = false;
      els.status.textContent = err.message;
      await loadItems();
    }
    return;
  }
  if (event.target.closest("a.shop")) return;
  const card = event.target.closest(".card");
  if (card && card.dataset.id) openCard(card.dataset.id);
});

els.cardSheet.addEventListener("click", async (event) => {
  if (event.target === els.cardSheet) {
    closeCard();
    return;
  }
  const btn = event.target.closest("[data-act]");
  if (!btn) return;
  event.preventDefault();
  try {
    await handleItemAction(btn.dataset.act, btn.dataset.id);
  } catch (err) {
    clearPending();
    els.status.hidden = false;
    els.status.textContent = err.message;
    await loadItems();
  }
});

document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (!els.addSheet.hidden || !els.codeSheet.hidden) return;
  if (!els.cardSheet.hidden) closeCard();
});

document.getElementById("toast-undo").addEventListener("click", async () => {
  if (!state.toast) return;
  try {
    await unreserve(state.toast.id);
  } catch (err) {
    els.status.hidden = false;
    els.status.textContent = err.message;
  }
});

document.querySelectorAll("[data-filter]").forEach((chip) => {
  chip.addEventListener("click", () => {
    state.filter = chip.dataset.filter;
    document.querySelectorAll("[data-filter]").forEach((node) => {
      node.classList.toggle("is-on", node === chip);
    });
    lastViewKey = "";
    render();
  });
});

document.querySelectorAll("[data-category]").forEach((chip) => {
  chip.addEventListener("click", () => {
    state.category = chip.dataset.category;
    document.querySelectorAll("[data-category]").forEach((node) => {
      node.classList.toggle("is-on", node === chip);
    });
    lastViewKey = "";
    render();
  });
});

function closeEditor() {
  state.editingId = null;
  showSheet(els.addSheet, false);
}

function fillEditor(item) {
  state.editingId = item ? item.id : null;
  els.addTitle.textContent = item ? "Изменить карточку" : "Новая карточка";
  els.submitAdd.textContent = item ? "Сохранить" : "Добавить в список";
  els.addForm.reset();
  els.noImage.checked = false;
  if (item) {
    els.url.value = item.url || "";
    els.titleField.value = item.title || "";
    els.priceField.value = item.price == null || item.price === "" ? "" : String(item.price);
    els.commentField.value = item.comment || "";
    els.categoryField.value = itemCategory(item);
    els.imageUrlField.value = item.imageUrl || "";
    els.previewStatus.textContent = "Можно поправить поля вручную или вставить новую ссылку.";
    showImagePreview(item.imageUrl || "");
    if (!item.imageUrl && item.url) {
      previewTimer = setTimeout(previewUrl, 50);
    }
  } else {
    els.imageUrlField.value = "";
    els.categoryField.value = "things";
    showImagePreview("");
    els.previewStatus.textContent =
      "Вставь ссылку — название, цена и картинка подтянутся сами, если магазин их отдаёт.";
  }
  showSheet(els.addSheet, true);
  els.url.focus();
}
document.getElementById("open-add").addEventListener("click", () => fillEditor(null));
document.getElementById("close-add").addEventListener("click", closeEditor);
document.getElementById("cancel-add").addEventListener("click", closeEditor);
document.getElementById("close-code").addEventListener("click", () => showSheet(els.codeSheet, false));
document.getElementById("cancel-code").addEventListener("click", () => showSheet(els.codeSheet, false));

els.url.addEventListener("input", () => {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(previewUrl, 450);
});
els.url.addEventListener("paste", () => {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(previewUrl, 50);
});
els.imageUrlField.addEventListener("input", () => {
  showImagePreview(els.imageUrlField.value.trim());
});

els.addForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  els.submitAdd.disabled = true;
  const payload = {
    url: els.url.value.trim(),
    title: els.titleField.value.trim(),
    price: els.priceField.value.trim(),
    comment: els.commentField.value.trim(),
    category: els.categoryField.value,
    imageUrl: els.noImage.checked ? "" : els.imageUrlField.value.trim() || state.previewImage,
  };
  try {
    if (state.editingId) {
      const data = await api(`/api/items/${state.editingId}`, {
        method: "PATCH",
        headers: headers(),
        body: JSON.stringify(payload),
      });
      const idx = state.items.findIndex((item) => item.id === state.editingId);
      if (idx >= 0) state.items[idx] = data.item;
    } else {
      const data = await api("/api/items", {
        method: "POST",
        headers: headers(),
        body: JSON.stringify(payload),
      });
      state.items.push(data.item);
    }
    closeEditor();
    render();
  } catch (err) {
    els.previewStatus.textContent = err.message;
  } finally {
    els.submitAdd.disabled = false;
  }
});

els.manageToggle.addEventListener("click", () => {
  if (state.isHost) {
    setHostMode(false);
    render();
    return;
  }
  els.codeError.hidden = true;
  els.codeForm.reset();
  showSheet(els.codeSheet, true);
  els.codeField.focus();
});

els.codeForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const code = els.codeField.value.trim();
  try {
    const data = await api("/api/auth", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ manageCode: code }),
    });
    if (!data.ok) throw new Error("Неверный код. Нужно: wishlist");
    setHostMode(true, code);
    showSheet(els.codeSheet, false);
    render();
  } catch (err) {
    els.codeError.hidden = false;
    els.codeError.textContent = err.message;
  }
});

async function boot() {
  await loadConfig();
  if (state.manageCode) {
    try {
      const data = await api("/api/auth", {
        method: "POST",
        headers: headers(),
        body: JSON.stringify({ manageCode: state.manageCode }),
      });
      setHostMode(Boolean(data.ok), data.ok ? state.manageCode : "");
    } catch {
      setHostMode(false);
    }
  } else {
    setHostMode(false);
  }
  await loadItems();
  setInterval(() => {
    if (state.pending || state.editingId || !els.addSheet.hidden) return;
    loadItems().catch(() => {});
  }, 4000);
}

boot().catch((err) => {
  els.status.hidden = false;
  els.status.textContent = err.message;
});
