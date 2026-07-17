/**
 * Viewer backend API client.
 *
 * All endpoints use relative URLs. In dev, the Vite proxy forwards `/api` and
 * `/media` to the Python backend on 127.0.0.1:9422 (see vite.config.ts). In
 * production, the backend serves the built React app and the `/api` routes
 * itself, so relative URLs resolve correctly in both environments.
 *
 * The Feed/Stats interfaces mirror the SQLite schema in src/viewer/backend/schema.py.
 * Backend endpoints are implemented in T11; the contracts here are the source of
 * truth that T11 must satisfy.
 */

/** A single feed row, as returned by list endpoints (no raw_json). */
export interface Feed {
  id: string
  /** Numeric QQ guild/community id; scopes media URLs (G7: /media/<guild_id>/<file>). */
  guild_id: string
  create_time: number | null
  title_text: string | null
  author_nick: string | null
  author_id: string | null
  author_avatar: string | null
  like_count: number
  comment_count: number
  image_count: number
  video_count: number
  /** Thumbnail filename from the list endpoint's first_media subquery. */
  first_media?: string | null
}

/** A guild row, as returned by GET /api/guilds. */
export interface Guild {
  guild_id: string
  guild_number: string | null
  name: string | null
  feeds: number
}

/** A single media row, as returned in FeedDetail.media. */
export interface Media {
  file: string | null
  url: string | null
  /** Discriminator: "image" or "video" (from media_index.jsonl). */
  type: string | null
  size: number | null
}

/** A single feed with parsed raw_json payload, as returned by detail endpoint. */
export interface FeedDetail extends Feed {
  raw_json: Record<string, unknown> | null
  indexed_at: string | null
  /** Media attachments; only present on detail responses. */
  media?: Media[]
}

/** A single comment, as returned by GET /api/feed/<id>/comments. */
export interface Comment {
  id: string
  create_time: number | null
  author_nick: string | null
  author_avatar: string | null
  content_text: string | null
  ip_location: string | null
  like_count: number
  reply_count: number
  parent_id: string | null
  sequence: number | null
}

/** Stats summary, as returned by GET /api/stats. */
export interface Stats {
  total_feeds: number
  total_media: number
  last_indexed_at: string | null
  [key: string]: unknown
}

/** Generic rebuild ack, as returned by POST /api/rebuild. */
export interface RebuildResult {
  ok: boolean
  [key: string]: unknown
}

/** Default page size; mirrors conf/viewer.conf.json `page_size`. */
export const DEFAULT_PAGE_SIZE = 20

/**
 * Wrapped fetch that throws on non-2xx. The thrown Error carries the response
 * status and (when available) the response body excerpt so callers can render
 * useful error UIs. Network errors propagate from fetch directly.
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
    const excerpt = body.length > 200 ? `${body.slice(0, 200)}…` : body
    const detail = excerpt ? `: ${excerpt}` : ''
    throw new Error(`HTTP ${res.status} ${res.statusText}${detail}`)
  }
  // 204 No Content / empty body → caller asked for JSON, fall through to parse.
  return (await res.json()) as T
}

/**
 * GET /api/feeds?page={page}&size={size}&guild={guildId} → list of feeds, newest first.
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
  return request<Feed[]>(`/api/feeds?${params.toString()}`)
}

/**
 * GET /api/feed/{feedId} → a single feed with its parsed raw_json payload.
 */
export async function fetchFeedDetail(feedId: string): Promise<FeedDetail> {
  return request<FeedDetail>(`/api/feed/${encodeURIComponent(feedId)}`)
}

/** GET /api/feed/{feedId}/comments → list of comments with nested replies. */
export async function fetchComments(feedId: string): Promise<Comment[]> {
  return request<Comment[]>(`/api/feed/${encodeURIComponent(feedId)}/comments`)
}

/** GET /api/guilds → all known guilds with feed counts, ordered by feeds desc. */
export async function fetchGuilds(): Promise<Guild[]> {
  return request<Guild[]>('/api/guilds')
}

/**
 * Build a guild-scoped media URL. Backend serves `/media/<guild_id>/<file>`
 * (G7 — exactly two path segments). Returns null when there is no filename.
 * When `guildId` is missing the segment is empty, preserving legacy paths
 * for any pre-migration data still served from the flat layout.
 */
export function mediaUrl(
  guildId: string | null | undefined,
  file: string | null | undefined,
): string | null {
  if (!file) return null
  const gid = guildId || ''
  return `/media/${gid}/${file}`
}

/**
 * GET /api/search?q={query}&page={page}&size={size}&guild={guildId} → matching feeds.
 * Returns an empty array when no results or out-of-range page. Pass `guildId`
 * null/undefined for the cross-guild view.
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
  return request<Feed[]>(`/api/search?${params.toString()}`)
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
