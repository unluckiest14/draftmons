/* Team logos: choosing one, and showing it.
 *
 * The logo is uploaded to the league's own database rather than linked, which
 * is what makes it show up for everyone — a pasted link only works while
 * somebody else's host keeps serving it, and stops working the day they
 * reorganise their folders.
 *
 * The browser does the resizing. A phone photo is three megabytes and a team
 * logo needs about thirty kilobytes, so anything large is drawn onto a 256px
 * canvas and re-encoded before it is sent. That keeps the league file small,
 * and re-rasterising also drops EXIF and anything else riding along in the
 * original — though the server re-checks the bytes regardless, because it
 * cannot assume this code was what called it.
 *
 * Small files are passed through untouched, so an animated GIF stays animated.
 * A canvas would flatten it to its first frame.
 */

import { api, API } from './api.js';
import { h, clear } from './dom.js';
import * as session from './session.js';

/** Longest edge, in pixels, after downscaling. */
const MAX_EDGE = 256;

/* Under this, the file is sent as-is. Above it, it is redrawn and re-encoded.
 * The threshold is what preserves animation on the small GIFs people actually
 * use as logos. */
const PASS_THROUGH_BYTES = 128 * 1024;

const ACCEPTED = ['image/png', 'image/jpeg', 'image/gif', 'image/webp'];

/** The src for a team's logo, or null. Handles the `?api=` override. */
export function logoSrc(team) {
  const logo = team?.logo;
  if (!logo) return null;
  // An uploaded logo is a path on the API; a pasted one is already absolute.
  return logo.startsWith('/') ? `${API}${logo}` : logo;
}

/** An <img> for a team's logo, or a lettered placeholder when it has none. */
export function logoFor(team, className = 'team-logo') {
  const src = logoSrc(team);
  if (!src) {
    return h('span', {
      class: `${className} is-empty`,
      text: (team?.name || '?').trim().charAt(0).toUpperCase(),
      'aria-hidden': 'true',
    });
  }
  return h('img', {
    class: className,
    src,
    alt: `${team?.name || 'Team'} logo`,
    loading: 'lazy',
    decoding: 'async',
  });
}

function readAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error('That file could not be read.'));
    reader.readAsDataURL(file);
  });
}

async function bitmapFor(file) {
  if (typeof createImageBitmap === 'function') {
    try {
      return await createImageBitmap(file);
    } catch {
      /* fall through to the <img> route, which handles a few more formats */
    }
  }
  const url = URL.createObjectURL(file);
  try {
    const image = new Image();
    await new Promise((resolve, reject) => {
      image.onload = resolve;
      image.onerror = () => reject(new Error('That file is not an image this browser can read.'));
      image.src = url;
    });
    return image;
  } finally {
    URL.revokeObjectURL(url);
  }
}

/** A data: URL for the image, downscaled if it is bigger than it needs to be. */
export async function prepare(file) {
  if (!ACCEPTED.includes(file.type)) {
    throw new Error('Use a PNG, JPEG, GIF or WebP. SVG cannot be accepted — it can carry scripts.');
  }
  if (file.size <= PASS_THROUGH_BYTES) {
    // Small enough already, and sending it untouched is what keeps an
    // animated GIF animated.
    return readAsDataUrl(file);
  }

  const source = await bitmapFor(file);
  const width = source.width || source.naturalWidth;
  const height = source.height || source.naturalHeight;
  const scale = Math.min(1, MAX_EDGE / Math.max(width, height));

  const canvas = document.createElement('canvas');
  canvas.width = Math.max(1, Math.round(width * scale));
  canvas.height = Math.max(1, Math.round(height * scale));
  const context = canvas.getContext('2d');
  context.imageSmoothingQuality = 'high';
  context.drawImage(source, 0, 0, canvas.width, canvas.height);
  return canvas.toDataURL('image/png');
}

/**
 * The upload control for one team. Returns an element.
 * `onChange` runs after a successful upload or removal.
 */
export function logoEditor(season, team, onChange) {
  const root = h('div', { class: 'logo-editor' });
  const preview = h('div', { class: 'logo-preview' });
  const status = h('span', { class: 'editor-note' });
  const file = h('input', {
    type: 'file', accept: ACCEPTED.join(','), class: 'logo-file',
  });

  const paint = () => {
    clear(preview);
    preview.append(logoFor(team, 'team-logo is-large'));
  };
  paint();

  const say = (message, bad = false) => {
    status.textContent = message;
    status.classList.toggle('is-error', bad);
  };

  const send = async (chosen) => {
    if (!chosen) return;
    say('Preparing…');
    try {
      const data = await prepare(chosen);
      const result = await api(`/seasons/${season.id}/teams/${team.id}/logo`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', ...session.headers(season.id) },
        body: JSON.stringify({ data }),
      });
      // Take the versioned URL straight from the response, so the new logo
      // shows immediately instead of behind a cached copy of the old one.
      team.logo = result.logo;
      paint();
      say(`Uploaded — ${Math.round(result.bytes / 1024) || 1} KB. Everyone sees it now.`);
      onChange?.();
    } catch (error) {
      say(error.message, true);
    }
  };

  file.addEventListener('change', () => send(file.files[0]));

  const drop = h('label', { class: 'logo-drop' }, [
    h('span', { text: 'Upload a logo' }),
    h('span', { class: 'editor-note', text: 'PNG, JPEG, GIF or WebP. Large images are resized to 256px.' }),
    file,
  ]);
  for (const name of ['dragover', 'dragenter']) {
    drop.addEventListener(name, (event) => { event.preventDefault(); drop.classList.add('is-over'); });
  }
  for (const name of ['dragleave', 'drop']) {
    drop.addEventListener(name, () => drop.classList.remove('is-over'));
  }
  drop.addEventListener('drop', (event) => {
    event.preventDefault();
    send(event.dataTransfer.files[0]);
  });

  const remove = h('button', {
    class: 'button', type: 'button', text: 'Remove',
    onclick: async () => {
      try {
        await api(`/seasons/${season.id}/teams/${team.id}/logo`, {
          method: 'DELETE', headers: session.headers(season.id),
        });
        team.logo = null;
        paint();
        say('Removed.');
        onChange?.();
      } catch (error) {
        say(error.message, true);
      }
    },
  });

  root.append(
    h('div', { class: 'logo-row' }, [preview, drop, remove]),
    status,
  );
  return root;
}
