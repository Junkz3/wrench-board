// web/js/services/repairs.js
// Read-side data service for repairs. The CREATE repair POST stays in the
// feature modules for now (cloud contract: urlencoded body — see plan invariants).
import { apiGet } from "../shared/api.js";

export const listRepairs = () => apiGet("/pipeline/repairs");
export const getRepair = (id) => apiGet(`/pipeline/repairs/${encodeURIComponent(id)}`);
