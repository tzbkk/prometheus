import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

type RouteHandler = (url: string) => { status: number; body: unknown }

function jsonResponse(status: number, body: unknown) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? 'OK' : 'Error',
    json: async () => body,
    text: async () => JSON.stringify(body),
  }
}

const calls: string[] = []

function mockRoutes(routes: Record<string, RouteHandler>) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      calls.push(url)
      for (const [prefix, handler] of Object.entries(routes)) {
        if (url.startsWith(prefix)) {
          const { status, body } = handler(url)
          return jsonResponse(status, body)
        }
      }
      return jsonResponse(404, {
        error: { code: 'not_found', message: `no test route for ${url}` },
      })
    }),
  )
}

const FEED = {
  id: 'B_f1',
  guild_id: '7743321643036658',
  create_time: '1725148800',
  title_text: '你好世界',
  author_nick: '小明',
  author_id: '10001',
  author_avatar: null,
  like_count: 3,
  comment_count: 5,
  image_count: 2,
  video_count: 0,
  first_media: '/media/7743321643036658/ab/abcd1234.jpg',
}

beforeEach(() => {
  calls.length = 0
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('fetchFeeds', () => {
  it('unwraps the {"feeds": [...]} envelope', async () => {
    mockRoutes({
      '/api/feeds': () => ({ status: 200, body: { feeds: [FEED] } }),
    })
    const { fetchFeeds } = await import('@/lib/api')
    const feeds = await fetchFeeds(1, 20, null)
    expect(feeds).toEqual([FEED])
    expect(calls[0]).toBe('/api/feeds?page=1&size=20')
  })

  it('sends the guild param when provided', async () => {
    mockRoutes({
      '/api/feeds': () => ({ status: 200, body: { feeds: [] } }),
    })
    const { fetchFeeds } = await import('@/lib/api')
    await fetchFeeds(2, 10, '7743321643036658')
    expect(calls[0]).toBe('/api/feeds?page=2&size=10&guild=7743321643036658')
  })
})

describe('searchFeeds', () => {
  it('unwraps the envelope and encodes q/page/size/guild', async () => {
    mockRoutes({
      '/api/search': () => ({ status: 200, body: { feeds: [FEED] } }),
    })
    const { searchFeeds } = await import('@/lib/api')
    const results = await searchFeeds('你好', 2, 5, 'g1')
    expect(results).toEqual([FEED])
    expect(calls[0]).toBe(
      '/api/search?q=%E4%BD%A0%E5%A5%BD&page=2&size=5&guild=g1',
    )
  })
})

describe('fetchGuilds', () => {
  it('unwraps the {"guilds": [...]} envelope', async () => {
    mockRoutes({
      '/api/guilds': () => ({
        status: 200,
        body: { guilds: [{ guild_id: '7743321643036658', feeds: 10091 }] },
      }),
    })
    const { fetchGuilds } = await import('@/lib/api')
    expect(await fetchGuilds()).toEqual([
      { guild_id: '7743321643036658', feeds: 10091 },
    ])
  })
})

describe('fetchComments', () => {
  it('unwraps the {"comments": [...]} envelope', async () => {
    const comment = {
      id: 'c_1',
      feed_id: 'B_f1',
      create_time: 1725148800,
      author_nick: '小红',
      author_avatar: null,
      content_text: '前排',
      like_count: 1,
      reply_count: 0,
      sequence: 1,
      media: [],
    }
    mockRoutes({
      '/api/feed/B_f1/comments': () => ({
        status: 200,
        body: { comments: [comment] },
      }),
    })
    const { fetchComments } = await import('@/lib/api')
    expect(await fetchComments('B_f1')).toEqual([comment])
    expect(calls[0]).toBe('/api/feed/B_f1/comments')
  })
})

describe('fetchFeedDetail', () => {
  it('returns the detail payload with full-path media entries', async () => {
    const detail = {
      ...FEED,
      raw_json: { id: 'B_f1' },
      indexed_at: '2026-01-01T00:00:00Z',
      media: [
        { path: '/media/7743321643036658/ab/abcd1234.jpg', type: 'image', url: 'https://example.com/a.jpg' },
        { path: null, type: 'video', url: 'https://example.com/b.mp4' },
      ],
    }
    mockRoutes({
      '/api/feed/B_f1': () => ({ status: 200, body: detail }),
    })
    const { fetchFeedDetail } = await import('@/lib/api')
    const got = await fetchFeedDetail('B_f1')
    expect(got.media).toEqual(detail.media)
    expect(got.create_time).toBe('1725148800')
  })
})

describe('fetchStats', () => {
  it('returns the three-key stats object', async () => {
    mockRoutes({
      '/api/stats': () => ({
        status: 200,
        body: { feeds: 10091, comments: 20887, media: 30112 },
      }),
    })
    const { fetchStats } = await import('@/lib/api')
    expect(await fetchStats()).toEqual({
      feeds: 10091,
      comments: 20887,
      media: 30112,
    })
  })
})

describe('rebuild', () => {
  it('POSTs /api/rebuild and returns the ack', async () => {
    mockRoutes({
      '/api/rebuild': () => ({ status: 200, body: { accepted: true } }),
    })
    const { rebuild } = await import('@/lib/api')
    expect(await rebuild()).toEqual({ accepted: true })
    expect(calls[0]).toBe('/api/rebuild')
  })
})

describe('error envelope', () => {
  it('surfaces the backend error message on non-2xx', async () => {
    mockRoutes({
      '/api/feed/missing': () => ({
        status: 404,
        body: { error: { code: 'not_found', message: 'feed missing not found' } },
      }),
    })
    const { fetchFeedDetail } = await import('@/lib/api')
    await expect(fetchFeedDetail('missing')).rejects.toThrow(
      /HTTP 404.*feed missing not found/,
    )
  })

  it('falls back to a body excerpt for non-JSON errors', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: false,
        status: 502,
        statusText: 'Bad Gateway',
        json: async () => ({}),
        text: async () => 'upstream exploded',
      })),
    )
    const { fetchStats } = await import('@/lib/api')
    await expect(fetchStats()).rejects.toThrow(/HTTP 502.*upstream exploded/)
  })
})
