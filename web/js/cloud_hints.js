// Hints de plan injectés par le front-door cloud (UX free/pro).
//
// En mode managé, le cloud injecte un global `window.__wbPlanHints` dans le
// HTML proxié (ex. {plan:"free", packedOnly:true, hideUploads:true,
// stockDonorLimit:5}) et l'UI adapte son affichage : lock du start-diagnostic
// tant qu'un appareil déjà packé n'est pas sélectionné, masquage des boutons
// d'upload. En self-host le global n'existe pas → tous les helpers répondent
// "rien à restreindre" et l'UI est strictement inchangée.
//
// COSMÉTIQUE UNIQUEMENT — aucune logique de confiance ici : les vraies
// barrières sont les 402 du cloud côté serveur. Retirer ces hints à la main
// ne débloque rien, le serveur refuse pareil.

export function planHints() {
  return (typeof window !== "undefined" && window.__wbPlanHints) || null;
}

// Le plan ne peut lancer un diagnostic que sur un appareil déjà packé (✓).
export function packedOnly() {
  const h = planHints();
  return !!(h && h.packedOnly);
}

// Le plan ne peut pas ajouter de schématique/boardview → cacher les affordances.
export function hideUploads() {
  const h = planHints();
  return !!(h && h.hideUploads);
}
