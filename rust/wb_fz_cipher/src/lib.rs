//! Rust/PyO3 accelerator for the FZ-xor boardview decoder (`.fz`).
//!
//! Mirrors `api/board/parser/_fz_engine/cipher.py::decrypt_fz_xor` exactly
//! — byte-identical output is guaranteed (the shared decode cache relies on
//! determinism: same input + key produces the same bytes whether Python or
//! Rust runs the inner loop).
//!
//! The transform runs per byte over a sliding 16-byte window. The pure-Python
//! hot loop made tens of millions of `_rol32` (rotate-32) calls (~0.02 MB/s);
//! this runs in native u32.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

const WINDOW: usize = 16;
const ROUNDS: usize = 20;

/// 32-bit left rotate. Exact mirror of the Python `_rol32`: only the low 5 bits
/// of the count matter, and a count of 0 is the identity.
#[inline(always)]
fn rol32(v: u32, s: u32) -> u32 {
    let s = s & 31;
    if s == 0 {
        v
    } else {
        (v << s) | (v >> (32 - s))
    }
}

/// Decode an FZ-xor payload. `k` must hold 44 uint32 words (the expanded key).
fn decrypt(cipher: &[u8], k: &[u32]) -> Vec<u8> {
    let mut window = [0u8; WINDOW];
    let (mut n5, mut n4, mut n3, mut n2): (u32, u32, u32, u32) = (0, 0, 0, 0);
    let mut out = vec![0u8; cipher.len()];

    for (i, &b) in cipher.iter().enumerate() {
        n4 = n4.wrapping_add(k[0]);
        n2 = n2.wrapping_add(k[1]);
        for r in 1..=ROUNDS {
            let t4 = n4.wrapping_mul((n4 << 1).wrapping_add(1));
            let mix4 = rol32(t4, 5);
            let t2 = n2.wrapping_mul((n2 << 1).wrapping_add(1));
            let mix2 = rol32(t2, 5);
            let new_n5 = rol32(n5 ^ mix4, mix2 & 0xff).wrapping_add(k[r * 2]);
            let new_n3 = rol32(n3 ^ mix2, mix4 & 0xff).wrapping_add(k[r * 2 + 1]);
            // State rotate: (n2, n3, n4, n5) <- (new_n5, old_n2, new_n3, old_n4)
            let saved_n5 = new_n5;
            n5 = n4;
            n4 = new_n3;
            n3 = n2;
            n2 = saved_n5;
        }
        n5 = n5.wrapping_add(k[42]);
        out[i] = b ^ (n5 & 0xff) as u8;

        // Slide the window left; the new input byte lands at slot 15.
        window.copy_within(1..WINDOW, 0);
        window[WINDOW - 1] = b;
        // Reload the 4 accumulators as little-endian uint32s from the window.
        n5 = u32::from_le_bytes([window[0], window[1], window[2], window[3]]);
        n4 = u32::from_le_bytes([window[4], window[5], window[6], window[7]]);
        n3 = u32::from_le_bytes([window[8], window[9], window[10], window[11]]);
        n2 = u32::from_le_bytes([window[12], window[13], window[14], window[15]]);
    }
    out
}

#[pyfunction]
fn decrypt_fz_xor<'py>(
    py: Python<'py>,
    cipher: Vec<u8>,
    key: Vec<u32>,
) -> PyResult<Bound<'py, PyBytes>> {
    if key.len() != 44 {
        return Err(PyValueError::new_err(format!(
            "FZ-xor key must be 44 uint32 words, got {}",
            key.len()
        )));
    }
    let out = decrypt(&cipher, &key);
    Ok(PyBytes::new_bound(py, &out))
}

#[pymodule]
fn wb_fz_cipher(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(decrypt_fz_xor, m)?)?;
    Ok(())
}
