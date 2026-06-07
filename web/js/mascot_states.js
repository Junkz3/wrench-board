// Single source of truth for the mascot's animation states.
//
// `mascot.js` reads this to validate states; the gallery
// (`web/mascot_gallery.html`) reads the SAME list to render one preview card
// per animation. Add a state here + its keyframes in `web/styles/mascot.css`
// and it appears automatically everywhere — no duplication.
//
//   id    : the suffix used in the `is-<id>` CSS class (and setMascotState arg)
//   label : human name shown on the gallery card
//   kind  : "loop"     — runs forever (idle, thinking, scanning…)
//           "oneshot"  — plays once and rests (success, error, celebrating…);
//                        the gallery gets a Replay button for these
//   blurb : one-line description of what the animation does

export const MASCOT_STATES = [
  { id: "idle",        label: "Repos",        kind: "loop",    blurb: "Respiration + clignements" },
  { id: "thinking",    label: "Réflexion",    kind: "loop",    blurb: "Corps orange + points « ... »" },
  { id: "typing",      label: "Frappe",       kind: "loop",    blurb: "Tape sur un clavier à deux mains" },
  { id: "working",     label: "Travail",      kind: "loop",    blurb: "La clé tourne comme un ratchet" },
  { id: "scanning",    label: "Scan",         kind: "loop",    blurb: "Loupe qui balaye, tête penchée" },
  { id: "sleeping",    label: "Sommeil",      kind: "loop",    blurb: "Yeux fermés, Zzz qui flottent" },
  { id: "success",     label: "Succès",       kind: "oneshot", blurb: "Clé brandie + grand sourire" },
  { id: "celebrating", label: "Célébration",  kind: "oneshot", blurb: "Saut, confettis, joie++" },
  { id: "error",       label: "Erreur",       kind: "oneshot", blurb: "Flash rouge + clé qui retombe" },
  { id: "danger",      label: "Alerte",       kind: "oneshot", blurb: "Corps écarlate, shake, yeux en croix" },
];

// Convenience lookups derived from the list above.
export const MASCOT_STATE_IDS = MASCOT_STATES.map((s) => s.id);
export const MASCOT_LOOP_IDS = MASCOT_STATES.filter((s) => s.kind === "loop").map((s) => s.id);
export const MASCOT_ONESHOT_IDS = MASCOT_STATES.filter((s) => s.kind === "oneshot").map((s) => s.id);
