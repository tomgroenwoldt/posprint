"use strict";

// Proof of work, solved in the page.
//
// The server names a challenge and a difficulty; this searches for a counter
// whose SHA-256 starts with that many zero bits. Finding it is the cost -
// hundreds of thousands of hashes - and checking it is one hash, which is the
// asymmetry the whole idea rests on.
//
// SHA-256 is implemented here rather than called through crypto.subtle because
// subtle.digest is asynchronous: a promise per hash turns a quarter-million
// hashes into a quarter-million scheduler round trips, which takes minutes
// instead of under a second. This runs synchronously in chunks and yields
// between them, so the page keeps painting and can show progress.

const Pow = (() => {
  const K = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
    0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
    0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
    0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
    0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
    0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
    0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
    0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
    0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ]);

  // Reused across calls. A fresh allocation per hash is most of the cost at
  // this volume, and there is only ever one search running.
  const W = new Uint32Array(64);
  const H = new Uint32Array(8);

  function digest(bytes, length) {
    H.set([0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
           0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]);

    // Padding: the 0x80 terminator, zeroes, then the bit length as a 64-bit
    // big-endian integer in the last eight bytes.
    const padded = ((length + 9 + 63) >> 6) << 6;
    for (let i = length; i < padded; i += 1) bytes[i] = 0;
    bytes[length] = 0x80;
    const bits = length * 8;
    bytes[padded - 4] = (bits >>> 24) & 0xff;
    bytes[padded - 3] = (bits >>> 16) & 0xff;
    bytes[padded - 2] = (bits >>> 8) & 0xff;
    bytes[padded - 1] = bits & 0xff;

    for (let base = 0; base < padded; base += 64) {
      for (let i = 0; i < 16; i += 1) {
        const j = base + i * 4;
        W[i] = (bytes[j] << 24) | (bytes[j + 1] << 16) |
               (bytes[j + 2] << 8) | bytes[j + 3];
      }
      for (let i = 16; i < 64; i += 1) {
        const a = W[i - 15];
        const b = W[i - 2];
        const s0 = ((a >>> 7) | (a << 25)) ^ ((a >>> 18) | (a << 14)) ^ (a >>> 3);
        const s1 = ((b >>> 17) | (b << 15)) ^ ((b >>> 19) | (b << 13)) ^ (b >>> 10);
        W[i] = (W[i - 16] + s0 + W[i - 7] + s1) | 0;
      }

      let [a, b, c, d, e, f, g, h] = H;
      for (let i = 0; i < 64; i += 1) {
        const S1 = ((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^
                   ((e >>> 25) | (e << 7));
        const ch = (e & f) ^ (~e & g);
        const t1 = (h + S1 + ch + K[i] + W[i]) | 0;
        const S0 = ((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^
                   ((a >>> 22) | (a << 10));
        const maj = (a & b) ^ (a & c) ^ (b & c);
        const t2 = (S0 + maj) | 0;
        h = g; g = f; f = e; e = (d + t1) | 0;
        d = c; c = b; b = a; a = (t1 + t2) | 0;
      }
      H[0] = (H[0] + a) | 0; H[1] = (H[1] + b) | 0;
      H[2] = (H[2] + c) | 0; H[3] = (H[3] + d) | 0;
      H[4] = (H[4] + e) | 0; H[5] = (H[5] + f) | 0;
      H[6] = (H[6] + g) | 0; H[7] = (H[7] + h) | 0;
    }
    return H;
  }

  // Hex, for checking this implementation against a known-good one.
  function hex(text) {
    const bytes = new Uint8Array(text.length + 72);
    for (let i = 0; i < text.length; i += 1) bytes[i] = text.charCodeAt(i);
    const h = digest(bytes, text.length);
    let out = "";
    for (let i = 0; i < 8; i += 1) out += h[i].toString(16).padStart(8, "0");
    return out;
  }

  function leadingZeroBits(h) {
    let bits = 0;
    for (let i = 0; i < 8; i += 1) {
      if (h[i] !== 0) return bits + Math.clz32(h[i]);
      bits += 32;
    }
    return bits;
  }

  const CHUNK = 20000;      // hashes between yields: ~30ms of work

  /**
   * Search for a counter that answers `challenge` at `bits` difficulty.
   *
   * Chunked rather than run in a worker: the whole search is well under a
   * second, and yielding between chunks keeps the page painting without the
   * ceremony of a second script context. onProgress is called with the number
   * of hashes tried, which is what the button counts.
   */
  async function solve(challenge, bits, onProgress, signal) {
    const prefix = `${challenge}.`;
    // One buffer for the whole search, sized for the longest counter we could
    // reach. Only the digits change between attempts.
    const bytes = new Uint8Array(prefix.length + 96);
    for (let i = 0; i < prefix.length; i += 1) bytes[i] = prefix.charCodeAt(i);

    let counter = 0;
    for (;;) {
      for (let n = 0; n < CHUNK; n += 1, counter += 1) {
        const digits = String(counter);
        for (let i = 0; i < digits.length; i += 1) {
          bytes[prefix.length + i] = digits.charCodeAt(i);
        }
        if (leadingZeroBits(digest(bytes, prefix.length + digits.length)) >= bits) {
          return { counter, tried: counter + 1 };
        }
      }
      if (signal && signal.aborted) throw new Error("cancelled");
      if (onProgress) onProgress(counter);
      // A macrotask with no clamping. setTimeout would add its 4ms floor to
      // every chunk, which at this rate is most of the wall clock.
      await new Promise((resolve) => {
        const channel = new MessageChannel();
        channel.port1.onmessage = () => resolve();
        channel.port2.postMessage(null);
      });
    }
  }

  return { solve, hex, leadingZeroBits, digest };
})();
