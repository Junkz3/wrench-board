// SPDX-License-Identifier: Apache-2.0
// Camera picker + capture helpers for the LLM panel head.
//
// Uses a custom chip+popover dropdown (cohérent avec .llm-tier-chip) so
// the look matches the rest of the panel — the native <select> renders
// with the OS default which is jarring on the dark theme.
//
// Public surface :
//   - initCameraPicker(onChange) — wires the chip + popover, populates
//     from enumerateDevices(), restores the previous selection from
//     localStorage. `onChange(deviceId, label)` fires every time the
//     tech picks a different device (or "— aucune —").
//   - selectedCameraDeviceId / selectedCameraLabel — read picker state
//   - isCameraAvailable — gate the capabilities frame
//   - captureFrame({deviceId, mime, quality}) → Blob — Flow B snap. If
//     camera_preview is open and on the same device, draws from its
//     live <video> instead of opening a second getUserMedia.
//   - blobToBase64(Blob) → string

import { captureFromPreview, isPreviewOpen } from './camera_preview.js';

const LS_KEY = 'wrench_board.cameraDeviceId';

let _cachedDevices = [];
let _selectedDeviceId = '';
let _onChangeCb = null;

function _devicesById(id) {
  return _cachedDevices.find((d) => d.deviceId === id) || null;
}

function _labelFor(id) {
  if (!id) return '— aucune —';
  const d = _devicesById(id);
  return d ? (d.label || `Caméra ${id.slice(0, 6)}…`) : '— aucune —';
}

function _renderChipLabel() {
  const labelEl = document.getElementById('cameraChipLabel');
  if (labelEl) labelEl.textContent = _labelFor(_selectedDeviceId);
}

function _renderPopover() {
  const popover = document.getElementById('cameraPopover');
  if (!popover) return;
  popover.innerHTML = '';
  const noneBtn = document.createElement('button');
  noneBtn.type = 'button';
  noneBtn.setAttribute('role', 'menuitem');
  noneBtn.dataset.deviceId = '';
  noneBtn.textContent = '— aucune —';
  if (!_selectedDeviceId) noneBtn.classList.add('on');
  popover.appendChild(noneBtn);
  _cachedDevices.forEach((d) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.setAttribute('role', 'menuitem');
    btn.dataset.deviceId = d.deviceId;
    btn.textContent = d.label || `Caméra ${d.deviceId.slice(0, 6)}…`;
    if (d.deviceId === _selectedDeviceId) btn.classList.add('on');
    popover.appendChild(btn);
  });
}

function _setSelected(id) {
  _selectedDeviceId = id || '';
  try { localStorage.setItem(LS_KEY, _selectedDeviceId); } catch (_) { /* quota */ }
  _renderChipLabel();
  _renderPopover();
  if (_onChangeCb) _onChangeCb(_selectedDeviceId, selectedCameraLabel());
}

export async function initCameraPicker(onChange) {
  _onChangeCb = onChange || null;
  const chip = document.getElementById('cameraChip');
  const popover = document.getElementById('cameraPopover');
  if (!chip || !popover) return;

  // Trigger a perm prompt to unlock device labels. Without granted
  // permission, enumerateDevices returns empty labels.
  try {
    const probe = await navigator.mediaDevices.getUserMedia({ video: true });
    probe.getTracks().forEach((t) => t.stop());
  } catch (_) {
    // Permission denied or no camera — labels will be empty but the
    // picker still works (blank options) and the user can re-grant later.
  }

  await refreshDevices();
  if (navigator.mediaDevices.addEventListener) {
    navigator.mediaDevices.addEventListener('devicechange', refreshDevices);
  }

  // Restore previous selection if still present.
  let saved = '';
  try { saved = localStorage.getItem(LS_KEY) || ''; } catch (_) { /* ignore */ }
  if (saved && _cachedDevices.some((d) => d.deviceId === saved)) {
    _selectedDeviceId = saved;
  }
  _renderChipLabel();
  _renderPopover();

  // Chip toggles popover.
  chip.addEventListener('click', (e) => {
    e.stopPropagation();
    const open = !popover.hidden;
    if (open) {
      popover.hidden = true;
      chip.setAttribute('aria-expanded', 'false');
    } else {
      popover.hidden = false;
      chip.setAttribute('aria-expanded', 'true');
    }
  });
  // Popover delegated click on a menuitem button.
  popover.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-device-id]');
    if (!btn) return;
    _setSelected(btn.dataset.deviceId);
    popover.hidden = true;
    chip.setAttribute('aria-expanded', 'false');
  });
  // Outside-click + Escape close.
  document.addEventListener('click', (e) => {
    if (popover.hidden) return;
    if (popover.contains(e.target) || chip.contains(e.target)) return;
    popover.hidden = true;
    chip.setAttribute('aria-expanded', 'false');
  }, true);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !popover.hidden) {
      popover.hidden = true;
      chip.setAttribute('aria-expanded', 'false');
    }
  });
}

async function refreshDevices() {
  const all = await navigator.mediaDevices.enumerateDevices();
  _cachedDevices = all.filter((d) => d.kind === 'videoinput');
  // If the previously-selected device just disappeared, downgrade.
  if (_selectedDeviceId && !_cachedDevices.some((d) => d.deviceId === _selectedDeviceId)) {
    _setSelected('');
    return;
  }
  _renderChipLabel();
  _renderPopover();
}

export function selectedCameraDeviceId() {
  return _selectedDeviceId;
}

export function selectedCameraLabel() {
  return _selectedDeviceId ? _labelFor(_selectedDeviceId) : '';
}

export function isCameraAvailable() {
  return Boolean(_selectedDeviceId);
}

export async function captureFrame({ deviceId, mime = 'image/jpeg', quality = 0.92 }) {
  // If the live preview is open on the same device, snap from its
  // existing stream instead of paying another getUserMedia.
  if (isPreviewOpen()) {
    const blob = await captureFromPreview(mime, quality);
    if (blob) return blob;
  }
  const stream = await navigator.mediaDevices.getUserMedia({
    video: { deviceId: { exact: deviceId } },
  });
  try {
    const video = document.createElement('video');
    video.srcObject = stream;
    video.muted = true;
    await video.play();
    await new Promise((r) => requestAnimationFrame(r));
    const canvas = document.createElement('canvas');
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    const ctx = canvas.getContext('2d');
    ctx.drawImage(video, 0, 0);
    return await new Promise((resolve) => canvas.toBlob(resolve, mime, quality));
  } finally {
    stream.getTracks().forEach((t) => t.stop());
  }
}

export async function blobToBase64(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onloadend = () => {
      const dataUrl = reader.result;
      const idx = dataUrl.indexOf(',');
      resolve(idx >= 0 ? dataUrl.slice(idx + 1) : '');
    };
    reader.onerror = reject;
    reader.readAsDataURL(blob);
  });
}
