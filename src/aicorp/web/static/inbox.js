/* Inbox: "select all on this page" checkbox. Works without it too (use "Mark all matching as read"). */
(() => {
  const all = document.getElementById("select-all");
  if (!all) return;
  all.addEventListener("change", () => document.querySelectorAll("input[name=ids]").forEach((c) => { c.checked = all.checked; }));
})();
