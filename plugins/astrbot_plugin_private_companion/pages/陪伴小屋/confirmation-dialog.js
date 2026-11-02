"use strict";
(() => {
  const dialog = document.querySelector("#confirmationDialog");
  const title = document.querySelector("#confirmationTitle");
  const message = document.querySelector("#confirmationMessage");
  const cancel = document.querySelector("#confirmationCancel");
  const accept = document.querySelector("#confirmationAccept");
  const save = document.querySelector("#confirmationSave");
  const details = document.querySelector("#confirmationDetails");
  const status = document.querySelector("#confirmationStatus");
  const queue = [];
  let active = null;
  let busy = false;

  if (!dialog || !title || !message || !cancel || !accept || typeof dialog.showModal !== "function") {
    window.CompanionConfirm = () => Promise.resolve(false);
    return;
  }

  function showNext() {
    if (active || !queue.length) return;
    active = queue.shift();
    title.textContent = active.options.title;
    message.textContent = active.options.message;
    cancel.textContent = active.options.cancelText;
    accept.textContent = active.options.confirmText;
    if (save) {
      save.hidden = !active.options.saveText;
      save.textContent = active.options.saveText;
    }
    dialog.dataset.hasSave = String(!!active.options.saveText);
    if (status) { status.textContent = ""; status.hidden = true; }
    if (details) {
      details.textContent = "";
      details.hidden = !active.options.details.length;
      for (const group of active.options.details) {
        const heading = document.createElement("h3");
        heading.textContent = group.title;
        details.appendChild(heading);
        const list = document.createElement("ul");
        for (const field of group.fields) {
          const item = document.createElement("li");
          item.textContent = field;
          list.appendChild(item);
        }
        details.appendChild(list);
      }
    }
    dialog.dataset.intent = active.options.danger ? "danger" : "normal";
    dialog.showModal();
    cancel.focus({preventScroll: true});
  }

  function finish(confirmed) {
    if (!active || busy) return;
    const request = active;
    active = null;
    dialog.close(confirmed ? "confirm" : "cancel");
    request.resolve(confirmed);
    Promise.resolve().then(showNext);
  }

  async function act(callback, saving) {
    if (!active || busy) return;
    if (typeof callback !== "function") { finish(true); return; }
    busy = true;
    for (const button of [cancel, accept, save]) if (button) button.disabled = true;
    dialog.setAttribute("aria-busy", "true");
    if (status) { status.hidden = false; status.dataset.error = "false"; status.textContent = saving ? "正在保存，请稍候…" : "正在放弃改动…"; }
    const label = save?.textContent;
    if (saving && save) save.textContent = "正在保存…";
    let result;
    try { result = await callback(); }
    catch { result = {error: "操作未完成，改动已保留，请回到配置中检查。"}; }
    busy = false;
    for (const button of [cancel, accept, save]) if (button) button.disabled = false;
    dialog.removeAttribute("aria-busy");
    if (saving && save) save.textContent = label;
    if (result === true) finish(true);
    else if (status) {
      status.hidden = false; status.dataset.error = "true";
      status.textContent = result?.error || "操作尚未完成，请回到配置中检查。";
    }
  }

  window.CompanionConfirm = request => {
    const options = typeof request === "string" ? {message: request} : request || {};
    const content = typeof options.message === "string" ? options.message.trim() : "";
    if (!content) return Promise.resolve(false);
    return new Promise(resolve => {
      queue.push({
        options: {
          title: typeof options.title === "string" && options.title.trim() ? options.title.trim() : "请确认此操作",
          message: content,
          confirmText: typeof options.confirmText === "string" && options.confirmText.trim() ? options.confirmText.trim() : "确认",
          cancelText: typeof options.cancelText === "string" && options.cancelText.trim() ? options.cancelText.trim() : "取消",
          danger: options.danger === true,
          details: Array.isArray(options.details) ? options.details : [],
          saveText: typeof options.onSave === "function" ? options.saveText || "保存并切换" : "",
          onSave: options.onSave,
          onConfirm: options.onConfirm,
        },
        resolve,
      });
      showNext();
    });
  };

  cancel.addEventListener("click", () => finish(false));
  accept.addEventListener("click", () => act(active?.options.onConfirm, false));
  save?.addEventListener("click", () => act(active?.options.onSave, true));
  dialog.addEventListener("cancel", event => {
    event.preventDefault();
    finish(false);
  });
  dialog.addEventListener("close", () => {
    if (active) finish(false);
  });
  dialog.addEventListener("click", event => {
    if (event.target !== dialog) return;
    const bounds = dialog.getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right
        || event.clientY < bounds.top || event.clientY > bounds.bottom) finish(false);
  });
})();
