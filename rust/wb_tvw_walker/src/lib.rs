//! Accélérateur Rust/PyO3 du hot-loop de walk des pins `.tvw`.
//!
//! Réplique EXACTEMENT `api/board/parser/_tvw_engine/walker.py` :
//!   - `_read_pin_record` (record pin variable, avec extension sub_a/sub_b/sub_c)
//!   - `_is_plausible_pin`
//!   - `_try_walk_pins_at` (header de section + boucle de records + walk partiel)
//!
//! Le profil Python : `_try_walk_pins_at` appelé ~32 686 fois (scan brute-force
//! d'offsets), `_read_pin_record` ~1,96 s, des millions de `_u8`/`_u32`/`_i32`.
//! Ici en u32/i32 natif. Le buffer est emprunté ZÉRO-COPIE (`&[u8]`) — crucial,
//! car la fonction est rappelée des dizaines de milliers de fois sur le même
//! buffer multi-Mo (le copier à chaque appel ruinerait le gain).
//!
//! Sortie record-identique au Python (le mapping Board en dépend).

use pyo3::prelude::*;
use pyo3::types::{PyList, PyTuple};

const MAX_COORD_CMILS: i64 = 5_000_000;

#[inline(always)]
fn rd_u32(buf: &[u8], off: usize) -> Option<u32> {
    let end = off.checked_add(4)?;
    if end > buf.len() {
        return None;
    }
    Some(u32::from_le_bytes([buf[off], buf[off + 1], buf[off + 2], buf[off + 3]]))
}

#[inline(always)]
fn rd_i32(buf: &[u8], off: usize) -> Option<i32> {
    let end = off.checked_add(4)?;
    if end > buf.len() {
        return None;
    }
    Some(i32::from_le_bytes([buf[off], buf[off + 1], buf[off + 2], buf[off + 3]]))
}

#[inline(always)]
fn rd_u8(buf: &[u8], off: usize) -> Option<u8> {
    buf.get(off).copied()
}

struct Pin {
    part_index: u32,
    pin_local: u32,
    x: i32,
    y: i32,
    flag1: u8,
    flag3: u8,
    raw_size: usize,
    pad: [i32; 4],
    has_bbox: bool,
}

/// Mirroir de `_read_pin_record`. Retourne (Some(pin), new_off) ou (None, off).
/// Tout dépassement de borne (équivalent IndexError/struct.error Python) → None.
fn read_pin_record(buf: &[u8], off0: usize, region_end: usize) -> (Option<Pin>, usize) {
    let base = off0;
    let mut off = off0;
    let (mut d1, mut d2, mut d3, mut d4) = (0i32, 0i32, 0i32, 0i32);
    let mut has_bbox = false;

    if off + 18 > region_end {
        return (None, off);
    }
    macro_rules! u32o { () => {{ match rd_u32(buf, off) { Some(v) => { off += 4; v } None => return (None, off) } }} }
    macro_rules! i32o { () => {{ match rd_i32(buf, off) { Some(v) => { off += 4; v } None => return (None, off) } }} }
    macro_rules! u8o { () => {{ match rd_u8(buf, off) { Some(v) => { off += 1; v } None => return (None, off) } }} }

    let part_idx = u32o!();
    let pin_local = u32o!();
    let x = i32o!();
    let y = i32o!();
    let flag1 = u8o!();
    let has_ext = u8o!();
    if has_ext != 0 {
        if off + 3 > region_end {
            return (None, off);
        }
        let sub_a = u8o!();
        if sub_a == 1 {
            if off + 12 > region_end {
                return (None, off);
            }
            off += 12;
        }
        let sub_b = u8o!();
        if sub_b != 0 {
            if off + 16 > region_end {
                return (None, off);
            }
            d1 = i32o!();
            d2 = i32o!();
            d3 = i32o!();
            d4 = i32o!();
            has_bbox = true;
        }
        let sub_c = u8o!();
        if sub_c != 0 {
            if off + 16 > region_end {
                return (None, off);
            }
            off += 16;
        }
    }
    if off + 1 > region_end {
        return (None, off);
    }
    let flag3 = u8o!();

    (
        Some(Pin {
            part_index: part_idx,
            pin_local,
            x,
            y,
            flag1,
            flag3,
            raw_size: off - base,
            pad: [d1, d2, d3, d4],
            has_bbox,
        }),
        off,
    )
}

#[inline]
fn is_plausible(p: &Pin) -> bool {
    if (p.x as i64).abs() > MAX_COORD_CMILS {
        return false;
    }
    if (p.y as i64).abs() > MAX_COORD_CMILS {
        return false;
    }
    if p.part_index >= (1u32 << 31) {
        return false;
    }
    if p.pin_local > 0x100000 {
        return false;
    }
    true
}

/// Mirroir de `_try_walk_pins_at`. None si pas de section pin valide.
fn try_walk(
    buf: &[u8],
    off: usize,
    region_end: usize,
    max_pin_count: u32,
    min_partial_ratio: f64,
) -> Option<(Vec<Pin>, usize, u32)> {
    if off + 12 > region_end {
        return None;
    }
    let first_count = rd_u32(buf, off)?;
    let pin_count = rd_u32(buf, off + 4)?;
    let mut off2 = off + 8;
    if pin_count == 0 {
        return Some((Vec::new(), off2, 0));
    }
    if pin_count > max_pin_count || first_count > max_pin_count {
        return None;
    }
    let _gap = rd_u32(buf, off2)?;
    off2 += 4;

    let mut pins: Vec<Pin> = Vec::new();
    let mut cur = off2;
    for _ in 0..pin_count {
        let (rec, new_off) = read_pin_record(buf, cur, region_end);
        match rec {
            Some(r) if is_plausible(&r) => {
                pins.push(r);
                cur = new_off;
            }
            _ => break,
        }
    }
    if pins.is_empty() {
        return None;
    }
    if pin_count >= 10 && (pins.len() as f64) < f64::max(2.0, pin_count as f64 * min_partial_ratio) {
        return None;
    }
    Some((pins, cur, pin_count))
}

/// Mirroir de `_looks_like_pin_record` (filtre rapide 18 octets).
#[inline]
fn looks_like_pin_record(buf: &[u8], off: usize, region_end: usize) -> bool {
    if off + 18 > region_end {
        return false;
    }
    let x = match rd_i32(buf, off + 8) {
        Some(v) => v,
        None => return false,
    };
    let y = match rd_i32(buf, off + 12) {
        Some(v) => v,
        None => return false,
    };
    (x as i64).abs() <= MAX_COORD_CMILS && (y as i64).abs() <= MAX_COORD_CMILS
}

/// Mirroir de `_looks_like_pin_section_header` (triage d'un header candidat).
#[inline]
fn looks_like_pin_section_header(buf: &[u8], off: usize, region_end: usize) -> bool {
    if off + 12 + 18 > region_end || off + 16 > buf.len() {
        return false;
    }
    // Pré-filtre rapide : les petits u32 ont leur octet de poids fort == 0.
    if buf[off + 3] != 0 || buf[off + 7] != 0 || buf[off + 11] != 0 {
        return false;
    }
    if buf[off + 15] != 0 {
        return false;
    }
    let first_count = match rd_u32(buf, off) {
        Some(v) => v,
        None => return false,
    };
    let pin_count = match rd_u32(buf, off + 4) {
        Some(v) => v,
        None => return false,
    };
    if pin_count < 1 || pin_count > 100_000 {
        return false;
    }
    if first_count > 100_000 {
        return false;
    }
    looks_like_pin_record(buf, off + 12, region_end)
}

/// Scan brute-force : balaie `[scan_start, scan_end)` par pas de `step`, triage
/// chaque candidat puis `try_walk`, et garde celui qui produit le PLUS de pins
/// (strictement). Réplique les deux boucles de scan Python (`_read_pins` step=4
/// et le scan d'apertures step=1). Retourne (best_off, pins, end, declared) ou None.
#[allow(clippy::too_many_arguments)]
fn scan_best(
    buf: &[u8],
    scan_start: usize,
    scan_end: usize,
    region_end: usize,
    step: usize,
    max_pin_count: u32,
    min_partial_ratio: f64,
) -> Option<(usize, Vec<Pin>, usize, u32)> {
    let mut best: Option<(usize, Vec<Pin>, usize, u32)> = None;
    let mut best_len = 0usize;
    let step = step.max(1);
    let mut cand = scan_start;
    while cand < scan_end {
        if looks_like_pin_section_header(buf, cand, region_end) {
            if let Some((pins, end, declared)) =
                try_walk(buf, cand, region_end, max_pin_count, min_partial_ratio)
            {
                if pins.len() > best_len {
                    best_len = pins.len();
                    best = Some((cand, pins, end, declared));
                }
            }
        }
        cand += step;
    }
    best
}

fn pins_to_pylist<'py>(py: Python<'py>, pins: Vec<Pin>) -> PyResult<Bound<'py, PyList>> {
    let list = PyList::empty_bound(py);
    for p in pins {
        let fields: [PyObject; 12] = [
            p.part_index.into_py(py),
            p.pin_local.into_py(py),
            p.x.into_py(py),
            p.y.into_py(py),
            p.flag1.into_py(py),
            p.flag3.into_py(py),
            (p.raw_size as u64).into_py(py),
            p.pad[0].into_py(py),
            p.pad[1].into_py(py),
            p.pad[2].into_py(py),
            p.pad[3].into_py(py),
            p.has_bbox.into_py(py),
        ];
        list.append(PyTuple::new_bound(py, fields))?;
    }
    Ok(list)
}

#[pyfunction]
#[pyo3(signature = (buf, scan_start, scan_end, region_end, step, max_pin_count=200_000, min_partial_ratio=0.5))]
fn scan_best_pin_section<'py>(
    py: Python<'py>,
    buf: &[u8],
    scan_start: usize,
    scan_end: usize,
    region_end: usize,
    step: usize,
    max_pin_count: u32,
    min_partial_ratio: f64,
) -> PyResult<PyObject> {
    match scan_best(buf, scan_start, scan_end, region_end, step, max_pin_count, min_partial_ratio) {
        None => Ok(py.None()),
        Some((best_off, pins, end_off, declared)) => {
            let list = pins_to_pylist(py, pins)?;
            Ok((best_off, list, end_off, declared).into_py(py))
        }
    }
}

#[pyfunction]
#[pyo3(signature = (buf, off, region_end, max_pin_count=200_000, min_partial_ratio=0.5))]
fn try_walk_pins_at<'py>(
    py: Python<'py>,
    buf: &[u8],
    off: usize,
    region_end: usize,
    max_pin_count: u32,
    min_partial_ratio: f64,
) -> PyResult<PyObject> {
    match try_walk(buf, off, region_end, max_pin_count, min_partial_ratio) {
        None => Ok(py.None()),
        Some((pins, end_off, declared)) => {
            let list = pins_to_pylist(py, pins)?;
            Ok((list, end_off, declared).into_py(py))
        }
    }
}

// === Network-names scan ==========================================================
// Mirroir de `_try_read_network_names` + `_scan_pascal_string_run` +
// `_is_plausible_net_name`. Scan brute-force par octet du dernier quart du fichier,
// gardant le plus long run de Pascal-strings « net-name » (ASCII imprimable). Les
// runs courts (cas dominant) n'allouent rien (Vec vide). Domine ~58% d'un gros .tvw.

#[inline]
fn is_plausible_net_name(s: &[u8]) -> bool {
    if s.is_empty() || s.len() > 64 {
        return false;
    }
    s.iter().all(|&b| (32..=126).contains(&b))
}

fn scan_pascal_run(buf: &[u8], start: usize, end: usize) -> Vec<String> {
    let mut names: Vec<String> = Vec::new();
    let mut cur = start;
    while cur < end {
        let n = buf[cur] as usize;
        if n == 0 {
            break;
        }
        if cur + 1 + n > end {
            break;
        }
        let s = &buf[cur + 1..cur + 1 + n];
        if !is_plausible_net_name(s) {
            break;
        }
        names.push(String::from_utf8_lossy(s).into_owned());
        cur += 1 + n;
    }
    names
}

fn try_read_netnames(buf: &[u8], after_layers: i64) -> Vec<String> {
    let end = buf.len();
    let mut best: Vec<String> = Vec::new();
    let margin = std::cmp::max(end / 4, 4096) as i64;
    let safe_window = end as i64 - margin;
    let mut window_start = std::cmp::min(after_layers, safe_window);
    if window_start < 0 {
        window_start = 0;
    }
    let window_start = window_start as usize;
    if window_start >= end.saturating_sub(8) {
        return Vec::new();
    }
    let mut cur = window_start;
    while cur + 4 < end {
        let names = scan_pascal_run(buf, cur, end);
        if names.len() >= best.len() && !names.is_empty() {
            best = names;
        }
        cur += 1;
    }
    best
}

#[pyfunction]
fn try_read_network_names<'py>(
    py: Python<'py>,
    buf: &[u8],
    after_layers: i64,
) -> PyResult<Bound<'py, PyList>> {
    Ok(PyList::new_bound(py, try_read_netnames(buf, after_layers)))
}

#[pymodule]
fn wb_tvw_walker(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(try_walk_pins_at, m)?)?;
    m.add_function(wrap_pyfunction!(scan_best_pin_section, m)?)?;
    m.add_function(wrap_pyfunction!(try_read_network_names, m)?)?;
    Ok(())
}
