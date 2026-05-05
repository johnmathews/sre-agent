#!/usr/bin/env node
// One-off script that emits placeholder PNG icons for the PWA manifest.
//
// Why this exists:
//   The manifest needs at least 192/512 sized PNGs and iOS needs a 180×180
//   apple-touch-icon. Real artwork can replace these any time — the script
//   produces a flat dark-blue square with a centred "SRE" wordmark drawn in
//   white using a tiny built-in 5×7 bitmap font. No external image deps.
//
// Run from the repo root: `node scripts/gen-placeholder-icons.mjs`.

import { writeFileSync, mkdirSync } from 'node:fs'
import { deflateSync } from 'node:zlib'

const BG = [11, 18, 32] // matches manifest theme_color #0b1220
const FG = [255, 255, 255]

// 5×7 bitmap font for S, R, E — enough to render "SRE".
// Each row is a 5-bit mask; rows are top-to-bottom.
const FONT = {
  S: [0b01111, 0b10000, 0b10000, 0b01110, 0b00001, 0b00001, 0b11110],
  R: [0b11110, 0b10001, 0b10001, 0b11110, 0b10100, 0b10010, 0b10001],
  E: [0b11111, 0b10000, 0b10000, 0b11110, 0b10000, 0b10000, 0b11111],
}

function makePng(size) {
  const buf = Buffer.alloc(size * size * 3)
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const i = (y * size + x) * 3
      buf[i] = BG[0]
      buf[i + 1] = BG[1]
      buf[i + 2] = BG[2]
    }
  }

  // Compute scale so "SRE" (3 glyphs × 5 px wide + 2 × 1 px gap = 17 px) fits
  // ~60% of the canvas width.
  const targetWidth = Math.floor(size * 0.62)
  const scale = Math.max(1, Math.floor(targetWidth / 17))
  const glyphW = 5 * scale
  const gap = scale
  const totalW = glyphW * 3 + gap * 2
  const totalH = 7 * scale
  const x0 = Math.floor((size - totalW) / 2)
  const y0 = Math.floor((size - totalH) / 2)

  const setPixel = (x, y) => {
    if (x < 0 || y < 0 || x >= size || y >= size) return
    const i = (y * size + x) * 3
    buf[i] = FG[0]
    buf[i + 1] = FG[1]
    buf[i + 2] = FG[2]
  }
  const drawGlyph = (rows, gx) => {
    for (let r = 0; r < 7; r++) {
      const row = rows[r]
      for (let c = 0; c < 5; c++) {
        if (row & (1 << (4 - c))) {
          for (let dy = 0; dy < scale; dy++) {
            for (let dx = 0; dx < scale; dx++) {
              setPixel(gx + c * scale + dx, y0 + r * scale + dy)
            }
          }
        }
      }
    }
  }

  drawGlyph(FONT.S, x0)
  drawGlyph(FONT.R, x0 + glyphW + gap)
  drawGlyph(FONT.E, x0 + 2 * (glyphW + gap))

  return encodePng(size, size, buf)
}

function encodePng(width, height, rgb) {
  const sig = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10])
  const ihdr = Buffer.alloc(13)
  ihdr.writeUInt32BE(width, 0)
  ihdr.writeUInt32BE(height, 4)
  ihdr[8] = 8 // bit depth
  ihdr[9] = 2 // color type RGB
  ihdr[10] = 0
  ihdr[11] = 0
  ihdr[12] = 0
  const rowBytes = 1 + width * 3
  const raw = Buffer.alloc(rowBytes * height)
  for (let y = 0; y < height; y++) {
    raw[y * rowBytes] = 0 // filter type: none
    rgb.copy(raw, y * rowBytes + 1, y * width * 3, y * width * 3 + width * 3)
  }
  const idatData = deflateSync(raw)
  return Buffer.concat([
    sig,
    chunk('IHDR', ihdr),
    chunk('IDAT', idatData),
    chunk('IEND', Buffer.alloc(0)),
  ])
}

function chunk(type, data) {
  const len = Buffer.alloc(4)
  len.writeUInt32BE(data.length, 0)
  const typeBuf = Buffer.from(type, 'ascii')
  const crc = Buffer.alloc(4)
  crc.writeUInt32BE(crc32(Buffer.concat([typeBuf, data])), 0)
  return Buffer.concat([len, typeBuf, data, crc])
}

const CRC_TABLE = (() => {
  const table = new Uint32Array(256)
  for (let n = 0; n < 256; n++) {
    let c = n
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
    table[n] = c >>> 0
  }
  return table
})()
function crc32(buf) {
  let c = 0xffffffff
  for (let i = 0; i < buf.length; i++) c = CRC_TABLE[(c ^ buf[i]) & 0xff] ^ (c >>> 8)
  return (c ^ 0xffffffff) >>> 0
}

mkdirSync('public', { recursive: true })
for (const [name, size] of [
  ['icon-192.png', 192],
  ['icon-512.png', 512],
  ['apple-touch-icon-180.png', 180],
]) {
  writeFileSync(`public/${name}`, makePng(size))
  // eslint-disable-next-line no-console
  console.log(`wrote public/${name} (${size}×${size})`)
}
