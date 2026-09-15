/* DOM helpers shared by the board and the planner.
 *
 * Pulled out of app.js when the planner arrived: both views build elements the
 * same way, and a second copy of `h` is how two views drift apart.
 */

/** Build an element. Text goes in via textContent, never innerHTML: every
 *  string here came off an HTTP response or a player's own typing. */
export function h(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'html') node.innerHTML = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? '' : value);
  }
  for (const child of [].concat(children)) {
    if (child !== null && child !== undefined && child !== false) node.append(child);
  }
  return node;
}

export const $ = (selector) => document.querySelector(selector);

export function clear(node) {
  node.textContent = '';
  return node;
}

/* A type badge. Deliberately takes no size argument: the old `small` flag is
 * what let the same type render at two sizes in two different views. */
export function typePip(name) {
  return h('span', { class: 'type', 'data-type': name, text: name });
}

export function placeholder(icon, title, body) {
  return h('div', { class: 'placeholder' }, [
    h('div', { class: 'placeholder-icon', text: icon }),
    h('div', { class: 'placeholder-title', text: title }),
    body ? h('p', { text: body }) : null,
  ]);
}

/** A pool entry's sprite, or a marked-up blank when PokeAPI has none. */
export function spriteFor(entry, className) {
  if (!entry?.sprite_url) {
    return h('div', { class: `${className} is-missing`, text: '?' });
  }
  return h('img', {
    class: className,
    src: entry.sprite_url,
    alt: entry.display_name,
    loading: 'lazy',
    decoding: 'async',
  });
}

/* Clipboard writes need a user gesture and a secure context, and the page is
 * served over plain http on localhost — a secure context, but only just. The
 * textarea fallback is for anyone who reaches this over http on a LAN
 * address, where navigator.clipboard is undefined. */
export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    try {
      const scratch = h('textarea', { text });
      scratch.style.cssText = 'position:fixed;top:-1000px;opacity:0';
      document.body.append(scratch);
      scratch.select();
      const ok = document.execCommand('copy');
      scratch.remove();
      return ok;
    } catch {
      return false;
    }
  }
}

/** Flash a button's label to confirm something happened, then put it back. */
export function flashLabel(button, message, ms = 1400) {
  const original = button.textContent;
  button.textContent = message;
  button.disabled = true;
  setTimeout(() => {
    button.textContent = original;
    button.disabled = false;
  }, ms);
}
