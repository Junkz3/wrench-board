// SPDX-License-Identifier: Apache-2.0
// Floating draggable camera preview frame.
//
// Rendered as a position:fixed PiP-style window mounted into <body> on
// first open. Streams a single getUserMedia track so the tech can keep
// an eye on what the agent will see when it calls cam_capture. Position
// + open state persist in localStorage. The frame can be dragged from
// its header anywhere in the viewport.
//
// Sharing with captureFrame() — when the preview is open, captureFrame
// draws from this stream instead of opening a second getUserMedia
// session (avoids the "device busy" / re-prompt pattern on some
// hardware).

const POS_KEY = 'wrench_board.cameraPreview.position';
const OPEN_KEY = 'wrench_board.cameraPreview.open';

let _root = null;
let _video = null;
let _stream = null;
let _currentDeviceId = '';
let _dragState = null;

function _ensureRoot() {
  if (_root) return _root;
  _root = document.createElement('div');
  _root.className = 'camera-preview';
  _root.id = 'cameraPreview';
  _root.hidden = true;
  _root.innerHTML = `
    <header class="camera-preview-head" id="cameraPreviewHead">
      <span class="camera-preview-title">Caméra · <span id="cameraPreviewLabel">—</span></span>
      <button class="camera-preview-close" type="button" aria-label="Fermer la preview">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"
             stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M6 6l12 12M18 6L6 18"/>
        </svg>
      </button>
    </header>
    <video class="camera-preview-video" id="cameraPreviewVideo"
           autoplay muted playsinline></video>
  `;
  document.body.appendChild(_root);

  _video = _root.querySelector('#cameraPreviewVideo');
  _root.querySelector('.camera-preview-close')
    .addEventListener('click', closePreview);

  // Drag — mousedown on header, mousemove on document, mouseup ends.
  const head = _root.querySelector('#cameraPreviewHead');
  head.addEventListener('mousedown', (e) => {
    if (e.button !== 0) return;
    const rect = _root.getBoundingClientRect();
    _dragState = {
      offsetX: e.clientX - rect.left,
      offsetY: e.clientY - rect.top,
    };
    document.body.style.userSelect = 'none';
    e.preventDefault();
  });
  document.addEventListener('mousemove', (e) => {
    if (!_dragState) return;
    let left = e.clientX - _dragState.offsetX;
    let top = e.clientY - _dragState.offsetY;
    // Clamp to viewport so the frame can't be dragged off-screen.
    const w = _root.offsetWidth;
    const h = _root.offsetHeight;
    left = Math.max(0, Math.min(window.innerWidth - w, left));
    top = Math.max(0, Math.min(window.innerHeight - h, top));
    _root.style.left = `${left}px`;
    _root.style.top = `${top}px`;
    _root.style.right = 'auto';
    _root.style.bottom = 'auto';
  });
  document.addEventListener('mouseup', () => {
    if (!_dragState) return;
    _dragState = null;
    document.body.style.userSelect = '';
    try {
      localStorage.setItem(POS_KEY, JSON.stringify({
        left: _root.style.left, top: _root.style.top,
      }));
    } catch (_) { /* ignore quota */ }
  });

  // Restore last position if any.
  try {
    const raw = localStorage.getItem(POS_KEY);
    if (raw) {
      const pos = JSON.parse(raw);
      if (pos.left) _root.style.left = pos.left;
      if (pos.top) _root.style.top = pos.top;
      _root.style.right = 'auto';
      _root.style.bottom = 'auto';
    }
  } catch (_) { /* ignore parse */ }

  return _root;
}

export async function openPreview(deviceId, label) {
  if (!deviceId) return false;
  _ensureRoot();
  if (_currentDeviceId !== deviceId || !_stream) {
    _stopStream();
    try {
      _stream = await navigator.mediaDevices.getUserMedia({
        video: { deviceId: { exact: deviceId } },
      });
    } catch (err) {
      console.error('[camera_preview] getUserMedia failed', err);
      return false;
    }
    _video.srcObject = _stream;
    _currentDeviceId = deviceId;
  }
  _root.hidden = false;
  const labelEl = _root.querySelector('#cameraPreviewLabel');
  if (labelEl) labelEl.textContent = label || 'caméra';
  try { localStorage.setItem(OPEN_KEY, '1'); } catch (_) { /* ignore */ }
  return true;
}

export function closePreview() {
  _stopStream();
  if (_root) _root.hidden = true;
  try { localStorage.setItem(OPEN_KEY, '0'); } catch (_) { /* ignore */ }
}

function _stopStream() {
  if (_stream) {
    _stream.getTracks().forEach((t) => t.stop());
    _stream = null;
  }
  if (_video) _video.srcObject = null;
  _currentDeviceId = '';
}

export function isPreviewOpen() {
  return Boolean(_root && !_root.hidden && _stream);
}

export function wasPreviewOpenLastSession() {
  try {
    return localStorage.getItem(OPEN_KEY) === '1';
  } catch (_) {
    return false;
  }
}

// If the picker changed and the preview is open, re-attach to the new
// device. If a different device, swap streams ; if same device, no-op.
export async function updatePreviewDevice(deviceId, label) {
  if (!isPreviewOpen()) return;
  if (!deviceId) {
    closePreview();
    return;
  }
  if (deviceId === _currentDeviceId) {
    const labelEl = _root.querySelector('#cameraPreviewLabel');
    if (labelEl) labelEl.textContent = label || 'caméra';
    return;
  }
  await openPreview(deviceId, label);  // openPreview swaps internally
}

// Snap a single frame from the live preview stream — avoids opening a
// second getUserMedia session when cam_capture fires while preview is on.
// Returns null if preview isn't open.
export async function captureFromPreview(mime = 'image/jpeg', quality = 0.92) {
  if (!isPreviewOpen() || !_video || !_video.videoWidth) return null;
  const canvas = document.createElement('canvas');
  canvas.width = _video.videoWidth;
  canvas.height = _video.videoHeight;
  const ctx = canvas.getContext('2d');
  ctx.drawImage(_video, 0, 0);
  return await new Promise((resolve) => canvas.toBlob(resolve, mime, quality));
}
