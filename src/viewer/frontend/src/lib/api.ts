/**
 * Viewer backend API client.
 *
 * All endpoints use relative URLs. In dev, the Vite proxy forwards `/api` and
 * `/media` to the Python backend on 127.0.0.1:9422 (see vite.config.ts). In
 * production, the backend serves the built React app and the `/api` routes
 * itself, so relative URLs resolve correctly in both environments.
 *
 * Contract notes (mirrors the live backend):
 * - List endpoints return envelope objects: {"feeds": [...]}, {"guilds": [...]},
 *   {"comments": [...]}. This module unwraps them so consumers get plain arrays.
 * - Errors are a bare status plus {"error": {"code", "message"}}.
 * - Media references (`first_media`, `Media.path`, `CommentMedia.path`) are
 *   ready-made hrefs of the form /media/<guild>/<shard>/<file> — use them
 *   directly as src/href values, never re-prefix.
 * - FeedItem.create_time is a decimal-seconds string ("" when the source value
 *   was null); Comment.create_time is a number.
 */

/** A single feed row, as returned by list endpoints (no raw_json). */
export interface Feed {
  id: string
  guild_id: string
  /** Decimal-seconds string; "" when the underlying value was null. */
  create_time: string
  title_text: string | null
  author_nick: string | null
  author_id: string | null
  author_avatar: string | null
  like_count: number
  comment_count: number
  image_count: number
  video_count: number
  /** Full media href (/media/<guild>/<shard>/<file>) or null; null on search. */
  first_media: string | null
}

/** A guild row, as returned by GET /api/guilds. */
export interface Guild {
  guild_id: string
  feeds: number
}

/** A single media attachment, as returned in FeedDetail.media. */
export interface Media {
  /** Full media href (/media/<guild>/<shard>/<file>) or null. */
  path: string | null
  /** Discriminator: "image" or "video" (from media_index.jsonl). */
  type: string | null
  /** Original remote URL the file was fetched from. */
  url: string | null
}

/** A single feed with parsed raw_json payload, as returned by detail endpoint. */
export interface FeedDetail extends Feed {
  raw_json: Record<string, unknown> | null
  indexed_at: string | null
  /** Full post body from the top-level contents envelope (untruncated). */
  content_text?: string | null
  /** Media attachments; only present on detail responses. */
  media?: Media[]
}

/** A single comment media entry (image/sticker attached to a comment). */
export interface CommentMedia {
  /** Full media href (/media/<guild>/<shard>/<file>) or null. */
  path: string | null
  file: string | null
  url: string | null
  type: string | null
  width: number | null
  height: number | null
}

/** A single comment, as returned by GET /api/feed/<id>/comments (flat c_ list). */
export interface Comment {
  id: string
  feed_id: string
  create_time: number | null
  author_nick: string | null
  author_avatar: string | null
  content_text: string | null
  like_count: number
  reply_count: number
  sequence: number | null
  /** Inline image attachments (comment images feature). */
  media?: CommentMedia[]
}

/** Stats summary, as returned by GET /api/stats (comments count includes replies). */
export interface Stats {
  feeds: number
  comments: number
  media: number
}

/** Rebuild ack, as returned by POST /api/rebuild. */
export interface RebuildResult {
  accepted: boolean
}

/** Default page size; mirrors conf/viewer.conf.json `page_size`. */
export const DEFAULT_PAGE_SIZE = 20

/**
 * Wrapped fetch that throws on non-2xx. The thrown Error carries the response
 * status and, when the body is an error envelope, its message (falling back to
 * a body excerpt) so callers can render useful error UIs. Network errors
 * propagate from fetch directly.
 */
async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init)
  if (!res.ok) {
    let body = ''
    try {
      body = await res.text()
    } catch {
      // Response body already consumed or stream errored; ignore.
    }
    let detail = ''
    try {
      const parsed = JSON.parse(body) as { error?: { message?: string } }
      detail = parsed.error?.message ?? ''
    } catch {
      const excerpt = body.length > 200 ? `${body.slice(0, 200)}…` : body
      detail = excerpt
    }
    const suffix = detail ? `: ${detail}` : ''
    throw new Error(`HTTP ${res.status} ${res.statusText}${suffix}`)
  }
  // 204 No Content / empty body → caller asked for JSON, fall through to parse.
  return (await res.json()) as T
}

/**
 * GET /api/feeds?page={page}&size={size}&guild={guildId} → feeds, newest first.
 * Returns an empty array on out-of-range pages. Pass `guildId` null/undefined
 * for the cross-guild view (no `guild` query param sent).
 */
export async function fetchFeeds(
  page = 1,
  size = DEFAULT_PAGE_SIZE,
  guildId?: string | null,
): Promise<Feed[]> {
  const params = new URLSearchParams({ page: String(page), size: String(size) })
  if (guildId) params.set('guild', guildId)
  const data = await request<{ feeds: Feed[] }>(`/api/feeds?${params.toString()}`)
  return data.feeds
}

/**
 * GET /api/feed/{feedId} → a single feed with its parsed raw_json payload.
 */
export async function fetchFeedDetail(feedId: string): Promise<FeedDetail> {
  return request<FeedDetail>(`/api/feed/${encodeURIComponent(feedId)}`)
}

/** GET /api/feed/{feedId}/comments → flat list of c_ comments for the feed. */
export async function fetchComments(feedId: string): Promise<Comment[]> {
  const data = await request<{ comments: Comment[] }>(
    `/api/feed/${encodeURIComponent(feedId)}/comments`,
  )
  return data.comments
}

/** GET /api/guilds → all known guilds with feed counts, ordered by feeds desc. */
export async function fetchGuilds(): Promise<Guild[]> {
  const data = await request<{ guilds: Guild[] }>('/api/guilds')
  return data.guilds
}

/**
 * GET /api/search?q={query}&page={page}&size={size}&guild={guildId} → matching
 * feeds. Returns an empty array when no results or out-of-range page. Pass
 * `guildId` null/undefined for the cross-guild view.
 */
export async function searchFeeds(
  query: string,
  page = 1,
  size = DEFAULT_PAGE_SIZE,
  guildId?: string | null,
): Promise<Feed[]> {
  const params = new URLSearchParams({
    q: query,
    page: String(page),
    size: String(size),
  })
  if (guildId) params.set('guild', guildId)
  const data = await request<{ feeds: Feed[] }>(`/api/search?${params.toString()}`)
  return data.feeds
}

/** GET /api/stats → ingestion/index summary counts. */
export async function fetchStats(): Promise<Stats> {
  return request<Stats>('/api/stats')
}

/** POST /api/rebuild → trigger a full re-ingestion; returns ack. */
export async function rebuild(): Promise<RebuildResult> {
  return request<RebuildResult>('/api/rebuild', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  })
}
