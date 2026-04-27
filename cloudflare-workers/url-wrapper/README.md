# Patchbay Relay URL wrapper

A small Cloudflare Worker that wraps custom URL schemes (`obsidian://`, `things://`, `shortcuts://`, etc.) in plain `https://` links so Telegram and other chat apps recognize them as tappable.

When an agent running on your Mac wants to send you a link to a note in your Obsidian vault, it cannot send `obsidian://open?vault=...` directly because Telegram does not render custom schemes. Instead it sends `https://your-domain.example/obs/<vault>/<path>`. Tapping the link in Telegram opens it in the browser, which serves a tiny page that immediately redirects to the real `obsidian://` URI through `meta refresh` and a JS fallback. The note opens in Obsidian on your phone.

The Worker is intentionally tiny (under 150 lines) so you can read the whole thing and deploy your own copy on a domain you control. There is no shared service.

## Routes

| Route | Redirects to |
| --- | --- |
| `/obs/<vault>/<path>` | `obsidian://open?vault=<vault>&file=<path>` |
| `/raw/<base64url>` | Any custom scheme (base64url-encoded) |
| `/` | Usage page |

## Deploy

You will need a Cloudflare account and the [Wrangler CLI](https://developers.cloudflare.com/workers/wrangler/install-and-update/).

```bash
cd cloudflare-workers/url-wrapper
npm install
npx wrangler login         # one time
npx wrangler deploy
```

The first deploy gives you a `https://patchbay-url-wrapper.<your-account>.workers.dev` URL. To put it on your own domain (recommended for a tappable short URL like `go.example.com/obs/...`), add a Workers route:

```bash
npx wrangler routes create "go.example.com/*"
```

You will also need a CNAME (or proxy-orange-cloud A record) for `go.example.com` pointing at Cloudflare. The Workers route then takes over.

## Headless deploy with API tokens

If you do not want to use `wrangler login` (or you are deploying from a headless box), set `CLOUDFLARE_API_TOKEN` to a token with **Workers Scripts: Edit** and **Workers Routes: Edit** permissions on your zone:

```bash
CLOUDFLARE_API_TOKEN=... CLOUDFLARE_ACCOUNT_ID=... npx wrangler deploy
```

## Pointing Patchbay Relay at your domain

Once deployed, set the URL prefix as an environment variable so agents pick it up:

```bash
export PATCHBAY_URL_WRAPPER="https://go.example.com"
```

Agents that build Obsidian links should read this prefix and emit `${PATCHBAY_URL_WRAPPER}/obs/<vault>/<path>` instead of raw `obsidian://` URIs. The wiring on the agent side lives outside this Worker.

## Usage from anywhere

Once it is deployed, any code that wants to send a tappable Obsidian (or other-scheme) link from a chat message can construct one of the wrapped URLs by hand. The Worker has no auth, no logging, no state. It is a stateless string transformation served as a redirect.

## Custom schemes other than Obsidian

The `/raw/<base64url>` route accepts any URI. Encode the full URI (e.g. `things:///add?title=Buy+milk`) as base64url and tack it on. This is the escape hatch for one-offs without adding a dedicated route per scheme.

## License

MIT, same as the rest of Patchbay Relay.
