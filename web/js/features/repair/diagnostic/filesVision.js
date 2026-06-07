// Diagnostic chat — Files+Vision (Flow A macro upload + Flow B camera capture)
// (Phase D.6 extraction from llm.js). Owns the image side-channel of the chat:
// announcing camera capabilities to the backend, the optimistic image bubble +
// fullscreen modal in the log, the drag/drop/upload handler (Flow A), and the
// agent-triggered capture handler (Flow B).
//
// Transport goes through services/diagnosticSocket.js (sendDiagnostic /
// getDiagnosticWS) — the same live socket llm.js drives — so this module never
// holds its own `ws`. Camera plumbing comes from camera.js; log rows from
// chatLog.js. `t` resolves through the global window.t at call time.

import {
  blobToBase64,
  captureFrame,
  isCameraAvailable,
  selectedCameraDeviceId,
  selectedCameraLabel,
} from '../../../camera.js';
import { escapeHtml as escapeHTML } from '../../../shared/dom.js';
import { sendDiagnostic, getDiagnosticWS } from '../../../services/diagnosticSocket.js';
import { logSys } from './chatLog.js';

const t = (key, params) => (window.t ? window.t(key, params) : key);
const el = (id) => document.getElementById(id);

export const MAX_UPLOAD_BYTES = 5 * 1024 * 1024;  // 5MB raw, mirrors backend cap

function socketOpen() {
  const ws = getDiagnosticWS();
  return !!(ws && ws.readyState === WebSocket.OPEN);
}

// Announce camera availability so the backend gates cam_capture in the manifest
// (runtime_direct) and can short-circuit empty captures (managed runtime).
export function sendCapabilities() {
  if (!socketOpen()) return;
  sendDiagnostic({
    type: "client.capabilities",
    camera_available: isCameraAvailable(),
    selected_device_id: selectedCameraDeviceId(),
  });
}

// Optimistic image bubble in the chat log. URL is either a blob: URL
// (Flow A optimistic local render) or a /api/macros/... URL (replay).
function appendImageBubble(role, srcUrl, captionText) {
  const log = el("llmLog");
  if (!log) return;
  const row = document.createElement("div");
  row.className = `msg ${role} msg-image`;
  const roleLabel = role === "user" ? t('chat.roles.user') : t('chat.roles.agent');
  const img = document.createElement("img");
  img.src = srcUrl;
  img.alt = captionText || t('chat.image_bubble.alt');
  img.className = "llm-bubble-img";
  img.addEventListener("click", () => openImageModal(srcUrl, captionText));
  const cap = document.createElement("div");
  cap.className = "llm-bubble-caption";
  cap.textContent = captionText || "";
  row.innerHTML = `<span class="role">${escapeHTML(roleLabel)}</span>`;
  row.appendChild(img);
  if (captionText) row.appendChild(cap);
  log.appendChild(row);
  log.scrollTop = log.scrollHeight;
}

function openImageModal(srcUrl, captionText) {
  let modal = document.getElementById("llmImageModal");
  if (!modal) {
    modal = document.createElement("div");
    modal.id = "llmImageModal";
    modal.className = "llm-image-modal";
    modal.addEventListener("click", () => modal.remove());
    document.body.appendChild(modal);
  } else {
    modal.innerHTML = "";
  }
  const img = document.createElement("img");
  img.src = srcUrl;
  img.alt = captionText || "";
  modal.appendChild(img);
}

// Flow A — tech-initiated macro upload (button / drag-drop). Validates size +
// mime client-side, renders optimistically, then ships the base64 over the WS.
export async function handleMacroUpload(file) {
  if (!file) return;
  if (file.size > MAX_UPLOAD_BYTES) {
    logSys(t('chat.upload.too_large', { size: (file.size / 1024 / 1024).toFixed(1) }), true);
    return;
  }
  if (!["image/png", "image/jpeg"].includes(file.type)) {
    logSys(t('chat.upload.unsupported', { mime: file.type }), true);
    return;
  }
  if (!socketOpen()) {
    logSys(t('chat.upload.socket_closed'), true);
    return;
  }
  // Optimistic local render — blob URL stays valid for the page lifetime.
  const url = URL.createObjectURL(file);
  appendImageBubble("user", url, t('chat.image_bubble.macro_caption'));
  try {
    const base64 = await blobToBase64(file);
    sendDiagnostic({
      type: "client.upload_macro",
      base64,
      mime: file.type,
      filename: file.name || "macro.jpg",
    });
  } catch (err) {
    logSys(t('chat.upload.failed', { error: err.message || err }), true);
  }
}

// Flow B — agent called cam_capture. Snap from the metabar-selected device and
// post back client.capture_response (success, or empty so the backend's
// is_error response closes the loop on the agent side).
export async function handleCaptureRequest(payload) {
  const { request_id, reason } = payload;
  const deviceId = selectedCameraDeviceId();
  if (!deviceId) {
    logSys(t('chat.capture.no_camera'), true);
    sendDiagnostic({
      type: "client.capture_response",
      request_id, base64: "", mime: "", device_label: "",
    });
    return;
  }
  logSys(t('chat.capture.requested', { reason: reason || t('chat.capture.no_reason') }));
  try {
    const blob = await captureFrame({
      deviceId, mime: "image/jpeg", quality: 0.92,
    });
    if (!blob) throw new Error("canvas.toBlob returned null");
    const base64 = await blobToBase64(blob);
    // Optimistic render so the tech sees what the agent received.
    const url = URL.createObjectURL(blob);
    appendImageBubble("user", url, t('chat.image_bubble.capture_caption', { label: selectedCameraLabel() }));
    sendDiagnostic({
      type: "client.capture_response",
      request_id,
      base64,
      mime: "image/jpeg",
      device_label: selectedCameraLabel(),
    });
  } catch (err) {
    console.error("captureFrame failed", err);
    logSys(t('chat.capture.failed', { error: err.message || err }), true);
    sendDiagnostic({
      type: "client.capture_response",
      request_id, base64: "", mime: "", device_label: "",
    });
  }
}
