/* The draft league API client.
 *
 * Same-origin by default, because FastAPI serves this directory. `?api=` is
 * the escape hatch for running the frontend off a dev server on another port,
 * which then needs CORS enabled on the backend.
 */

export const API = new URLSearchParams(location.search).get('api') || '';

export async function api(path, options) {
  let response;
  try {
    response = await fetch(`${API}${path}`, options);
  } catch (cause) {
    /* fetch() rejects — rather than returning a status — when the request
     * never reached a server: nothing listening, DNS failure, connection
     * dropped mid-flight. Chrome words that as "Failed to fetch", which tells
     * a player nothing and reads like a bug in the page.
     *
     * The overwhelmingly common cause in this project is that the API is not
     * running, or is running bound to 127.0.0.1 while the player is on another
     * device, so the message says both. `offline` lets a caller tell this
     * apart from an HTTP error it might want to handle.
     */
    const error = new Error(
      'Cannot reach the league server. Check that it is running, and that you '
      + 'are using the address your commissioner gave you.',
    );
    error.offline = true;
    error.cause = cause;
    throw error;
  }

  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body.detail) message = typeof body.detail === 'string' ? body.detail : message;
    } catch {
      /* not JSON; the status line is the best we have */
    }
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return response.json();
}
