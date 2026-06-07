// web/js/services/packs.js
// Read-side data service for device knowledge packs. Wraps the /pipeline/packs
// endpoints behind named functions. Call sites never fetch directly.
import { apiGet } from "../shared/api.js";

export const listPacks = () => apiGet("/pipeline/packs");
export const getPackFull = (slug) => apiGet(`/pipeline/packs/${encodeURIComponent(slug)}/full`);
export const getPackGraph = (slug) => apiGet(`/pipeline/packs/${encodeURIComponent(slug)}/graph`);
