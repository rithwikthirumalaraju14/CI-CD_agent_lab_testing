const form = document.getElementById("capture-form");
if (form) {
  form.addEventListener("submit", () => {
    const button = document.getElementById("capture-button");
    button.disabled = true;
    button.textContent = "Collecting evidence…";
    document.getElementById("loading-message").hidden = false;
  });
}
