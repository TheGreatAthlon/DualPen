const A11Y_SUPPORT_STORAGE_KEY = "collab-editor:accessibilitySupport";
const FONT_FAMILY_STORAGE_KEY = "collab-editor:fontFamily";
const FONT_SIZE_STORAGE_KEY = "collab-editor:fontSize";

export const DEFAULT_FONT_SIZE = 14;
export const MIN_FONT_SIZE = 10;
export const MAX_FONT_SIZE = 32;

// Curated rather than free text, per the project plan - avoids a user
// typing a font that isn't actually monospace (breaking column alignment)
// or isn't installed on their system with no visible fallback indication.
// CSS generic families close each stack so something reasonable always
// renders even if none of the named fonts are present.
export const FONT_CHOICES: { label: string; family: string }[] = [
  { label: "Cascadia Code", family: '"Cascadia Code", Consolas, monospace' },
  { label: "Consolas", family: "Consolas, monospace" },
  { label: "Fira Code", family: '"Fira Code", monospace' },
  { label: "JetBrains Mono", family: '"JetBrains Mono", monospace' },
  { label: "Source Code Pro", family: '"Source Code Pro", monospace' },
  { label: "Courier New", family: '"Courier New", Courier, monospace' },
  { label: "System monospace", family: "ui-monospace, Menlo, Consolas, monospace" },
];

const DEFAULT_FONT_FAMILY = FONT_CHOICES[0].family;

export function loadAccessibilitySupportPref(): boolean {
  return window.localStorage.getItem(A11Y_SUPPORT_STORAGE_KEY) === "on";
}

export function saveAccessibilitySupportPref(on: boolean): void {
  window.localStorage.setItem(A11Y_SUPPORT_STORAGE_KEY, on ? "on" : "off");
}

export function loadFontFamily(): string {
  const saved = window.localStorage.getItem(FONT_FAMILY_STORAGE_KEY);
  // Only trust a saved value if it's still one of the curated choices - the
  // list can change between releases, and a stale/foreign value should fall
  // back to the default rather than being handed to Monaco as-is.
  const match = saved && FONT_CHOICES.find((f) => f.family === saved);
  return match ? match.family : DEFAULT_FONT_FAMILY;
}

export function saveFontFamily(family: string): void {
  window.localStorage.setItem(FONT_FAMILY_STORAGE_KEY, family);
}

export function loadFontSize(): number {
  const raw = window.localStorage.getItem(FONT_SIZE_STORAGE_KEY);
  const parsed = raw === null ? NaN : Number(raw);
  if (!Number.isFinite(parsed)) return DEFAULT_FONT_SIZE;
  return Math.min(MAX_FONT_SIZE, Math.max(MIN_FONT_SIZE, Math.round(parsed)));
}

export function saveFontSize(size: number): void {
  const clamped = Math.min(MAX_FONT_SIZE, Math.max(MIN_FONT_SIZE, Math.round(size)));
  window.localStorage.setItem(FONT_SIZE_STORAGE_KEY, String(clamped));
}

export interface SettingsPanelCallbacks {
  onAccessibilitySupportChange: (on: boolean) => void;
  onFontChange: (family: string, size: number) => void;
  onPresenceMutedChange: (muted: boolean) => void;
  onPresenceVolumeChange: (volume: number) => void;
  getPresenceMuted: () => boolean;
  getPresenceVolume: () => number;
}

/**
 * Consolidated settings dialog: accessibility mode, presence sound
 * mute/volume, and per-user font family/size with a live preview. Built on
 * a native <dialog>, created once and reused (mirrors chat.ts's ChatPanel),
 * so preview state doesn't need to be rebuilt on every open.
 */
export class SettingsPanel {
  private dialog: HTMLDialogElement;
  private previewEl: HTMLElement;
  private fontSelect: HTMLSelectElement;
  private sizeInput: HTMLInputElement;

  constructor(callbacks: SettingsPanelCallbacks) {
    const fontOptions = FONT_CHOICES.map(
      (f) => `<option value="${f.family.replace(/"/g, "&quot;")}">${f.label}</option>`,
    ).join("");

    this.dialog = document.createElement("dialog");
    this.dialog.className = "settings-dialog";
    this.dialog.setAttribute("aria-labelledby", "settings-dialog-heading");
    this.dialog.innerHTML = `
      <h2 id="settings-dialog-heading">Settings</h2>
      <div class="settings-section">
        <h3>Accessibility</h3>
        <label class="settings-checkbox-row">
          <input type="checkbox" id="settings-a11y-toggle" />
          Screen reader optimized mode
        </label>
      </div>
      <div class="settings-section">
        <h3>Presence sounds</h3>
        <label class="settings-checkbox-row">
          <input type="checkbox" id="settings-presence-mute-toggle" />
          Mute presence sounds
        </label>
        <label class="settings-row" for="settings-presence-volume-slider">
          Volume
          <input type="range" id="settings-presence-volume-slider" min="0" max="100" step="5" />
        </label>
      </div>
      <div class="settings-section">
        <h3>Editor font</h3>
        <label class="settings-row" for="settings-font-select">
          Font
          <select id="settings-font-select">${fontOptions}</select>
        </label>
        <label class="settings-row" for="settings-font-size-input">
          Size
          <span class="settings-stepper">
            <button type="button" id="settings-font-size-down" aria-label="Decrease font size">−</button>
            <input type="number" id="settings-font-size-input" min="${MIN_FONT_SIZE}" max="${MAX_FONT_SIZE}" />
            <button type="button" id="settings-font-size-up" aria-label="Increase font size">+</button>
          </span>
        </label>
        <p id="settings-font-preview" class="settings-font-preview">The quick brown fox jumps over the lazy dog.</p>
      </div>
      <div class="settings-buttons">
        <button type="button" id="settings-close-btn">Close</button>
      </div>
    `;
    document.body.appendChild(this.dialog);

    const a11yToggle = this.dialog.querySelector<HTMLInputElement>("#settings-a11y-toggle")!;
    const muteToggle = this.dialog.querySelector<HTMLInputElement>("#settings-presence-mute-toggle")!;
    const volumeSlider = this.dialog.querySelector<HTMLInputElement>("#settings-presence-volume-slider")!;
    this.fontSelect = this.dialog.querySelector<HTMLSelectElement>("#settings-font-select")!;
    this.sizeInput = this.dialog.querySelector<HTMLInputElement>("#settings-font-size-input")!;
    const sizeDown = this.dialog.querySelector<HTMLButtonElement>("#settings-font-size-down")!;
    const sizeUp = this.dialog.querySelector<HTMLButtonElement>("#settings-font-size-up")!;
    this.previewEl = this.dialog.querySelector<HTMLElement>("#settings-font-preview")!;
    const closeBtn = this.dialog.querySelector<HTMLButtonElement>("#settings-close-btn")!;

    a11yToggle.checked = loadAccessibilitySupportPref();
    a11yToggle.addEventListener("change", () => {
      saveAccessibilitySupportPref(a11yToggle.checked);
      callbacks.onAccessibilitySupportChange(a11yToggle.checked);
    });

    muteToggle.checked = callbacks.getPresenceMuted();
    muteToggle.addEventListener("change", () => {
      callbacks.onPresenceMutedChange(muteToggle.checked);
    });
    volumeSlider.value = String(Math.round(callbacks.getPresenceVolume() * 100));
    volumeSlider.addEventListener("input", () => {
      callbacks.onPresenceVolumeChange(Number(volumeSlider.value) / 100);
    });

    this.fontSelect.value = loadFontFamily();
    this.sizeInput.value = String(loadFontSize());
    this.updatePreview();

    const applyFontChange = () => {
      const family = this.fontSelect.value;
      const size = this.clampAndSyncSizeInput();
      saveFontFamily(family);
      saveFontSize(size);
      callbacks.onFontChange(family, size);
      this.updatePreview();
    };

    this.fontSelect.addEventListener("change", applyFontChange);
    this.sizeInput.addEventListener("change", applyFontChange);
    sizeDown.addEventListener("click", () => {
      this.sizeInput.value = String(Number(this.sizeInput.value) - 1);
      applyFontChange();
    });
    sizeUp.addEventListener("click", () => {
      this.sizeInput.value = String(Number(this.sizeInput.value) + 1);
      applyFontChange();
    });

    closeBtn.addEventListener("click", () => this.dialog.close());
  }

  private clampAndSyncSizeInput(): number {
    const clamped = Math.min(MAX_FONT_SIZE, Math.max(MIN_FONT_SIZE, Math.round(Number(this.sizeInput.value) || DEFAULT_FONT_SIZE)));
    this.sizeInput.value = String(clamped);
    return clamped;
  }

  private updatePreview(): void {
    this.previewEl.style.fontFamily = this.fontSelect.value;
    this.previewEl.style.fontSize = `${this.sizeInput.value}px`;
  }

  openFocused(): void {
    if (!this.dialog.open) this.dialog.showModal();
    this.fontSelect.focus();
  }
}
