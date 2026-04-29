export default {
  async fetch(request, env) {
    const TUNNEL_URL = env.TUNNEL_URL;
    const url = new URL(request.url);
    const targetUrl = TUNNEL_URL + url.pathname + url.search;

    const modifiedRequest = new Request(targetUrl, {
      method: request.method,
      headers: request.headers,
      body: request.method !== 'GET' && request.method !== 'HEAD' ? request.body : undefined,
    });

    try {
      const response = await fetch(modifiedRequest);
      const newResponse = new Response(response.body, {
        status: response.status,
        statusText: response.statusText,
        headers: response.headers,
      });
      newResponse.headers.set('Access-Control-Allow-Origin', '*');
      newResponse.headers.set('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS');
      newResponse.headers.set('Access-Control-Allow-Headers', 'Content-Type, X-Admin-Token');
      return newResponse;
    } catch (e) {
      return new Response('Push server unreachable: ' + e.message, { status: 502 });
    }
  }
};
