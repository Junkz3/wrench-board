// SPDX-License-Identifier: Apache-2.0
// Camera picker + capture helpers for the metabar.
//
// Initialized once at boot from main.js (initCameraPicker). Persists the
// chosen device id in localStorage. Exposes helpers for llm.js :
//   - selectedCameraDeviceId / selectedCameraLabel — read picker state
//   - isCameraAvailable — gate the capabilities frame
//   - captureFrame({deviceId, mime, quality}) → Blob — Flow B snap
//   - blobToBase64(Blob) → string — Flow A + Flow B encoding

const LS_KEY = 'microsolder.cameraDeviceId';

let _cachedDevices = [];
let _onChangeCb = null;

export async function initCameraPicker(onChange) {
  _onChangeCb = onChange || null;
  const select = document.getElementById('camera-picker');
  if (!select) return;

  // Trigger a perm prompt to unlock device labels (best-effort).
  // Without granted permission, enumerateDevices returns empty labels.
  try {
    const probe = await navigator.mediaDevices.getUserMedia({ video: true });
    probe.getTracks().forEach((t) => t.stop());
  } catch (_) {
    // Permission denied or no camera — labels will be empty but the picker
    // still works (blank options) and the user can re-grant later.
  }

  await refreshDevices();
  if (navigator.mediaDevices.addEventListener) {
    navigator.mediaDevices.addEventListener('devicechange', refreshDevices);
  }

  select.addEventListener('change', () => {
    localStorage.setItem(LS_KEY, select.value);
    if (_onChangeCb) _onChangeCb(select.value);
  });
}

async function refreshDevices() {
  const select = document.getElementById('camera-picker');
  if (!select) return;
  const all = await navigator.mediaDevices.enumerateDevices();
  _cachedDevices = all.filter((d) => d.kind === 'videoinput');
  const saved = localStorage.getItem(LS_KEY) || '';
  // Preserve "aucune" entry, replace the rest.
  while (select.options.length > 1) select.remove(1);
  _cachedDevices.forEach((d) => {
    const opt = document.createElement('option');
    opt.value = d.deviceId;
    opt.textContent = d.label || `Caméra ${d.deviceId.slice(0, 6)}…`;
    select.appendChild(opt);
  });
  // Restore previous selection if still present.
  if (saved && _cachedDevices.some((d) => d.deviceId === saved)) {
    select.value = saved;
  }
}

export function selectedCameraDeviceId() {
  const select = document.getElementById('camera-picker');
  return select ? select.value : '';
}

export function selectedCameraLabel() {
  const id = selectedCameraDeviceId();
  if (!id) return '';
  const d = _cachedDevices.find((x) => x.deviceId === id);
  return d ? (d.label || 'caméra') : '';
}

export function isCameraAvailable() {
  return Boolean(selectedCameraDeviceId());
}

export async function captureFrame({ deviceId, mime = 'image/jpeg', quality = 0.92 }) {
  const stream = await navigator.mediaDevices.getUserMedia({
    video: { deviceId: { exact: deviceId } },
  });
  try {
    const video = document.createElement('video');
    video.srcObject = stream;
    video.muted = true;
    await video.play();
    // Wait one frame so the video has a usable size.
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
      // reader.result is "data:image/jpeg;base64,XXXX" — strip the prefix.
      const dataUrl = reader.result;
      const idx = dataUrl.indexOf(',');
      resolve(idx >= 0 ? dataUrl.slice(idx + 1) : '');
    };
    reader.onerror = reject;
    reader.readAsDataURL(blob);
  });
}
