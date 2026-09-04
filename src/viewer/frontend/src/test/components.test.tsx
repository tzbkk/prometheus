import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { FeedCard } from '@/components/FeedCard'
import { MediaGrid } from '@/components/MediaGrid'
import { CommentList } from '@/components/CommentList'
import { Layout } from '@/components/Layout'
import { GuildProvider } from '@/lib/guild-context'
import type { Comment, Feed } from '@/lib/api'

function jsonResponse(status: number, body: unknown) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? 'OK' : 'Error',
    json: async () => body,
    text: async () => JSON.stringify(body),
  }
}

function makeFeed(overrides: Partial<Feed> = {}): Feed {
  return {
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
    ...overrides,
  }
}

function makeComment(overrides: Partial<Comment> = {}): Comment {
  return {
    id: 'c_1',
    feed_id: 'B_f1',
    create_time: 1725148800,
    author_nick: '小红',
    author_avatar: null,
    content_text: '前排围观',
    like_count: 1,
    reply_count: 2,
    sequence: 1,
    media: [],
    ...overrides,
  }
}

function testQueryClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
}

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.startsWith('/api/guilds')) {
        return jsonResponse(200, {
          guilds: [{ guild_id: '7743321643036658', feeds: 10091 }],
        })
      }
      if (url.startsWith('/api/stats')) {
        return jsonResponse(200, {
          feeds: 10091,
          comments: 20887,
          media: 30112,
        })
      }
      return jsonResponse(404, {
        error: { code: 'not_found', message: `no test route for ${url}` },
      })
    }),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('FeedCard', () => {
  it('uses first_media directly as the thumbnail src', () => {
    render(
      <MemoryRouter>
        <FeedCard feed={makeFeed()} />
      </MemoryRouter>,
    )
    const img = screen.getByRole('img', { name: '你好世界' })
    expect(img).toHaveAttribute('src', '/media/7743321643036658/ab/abcd1234.jpg')
  })

  it('renders no thumbnail when first_media is null (search results)', () => {
    render(
      <MemoryRouter>
        <FeedCard feed={makeFeed({ first_media: null })} />
      </MemoryRouter>,
    )
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
  })

  it('shows the placeholder for empty create_time strings', () => {
    render(
      <MemoryRouter>
        <FeedCard feed={makeFeed({ create_time: '' })} />
      </MemoryRouter>,
    )
    expect(screen.getByText('—')).toBeInTheDocument()
  })
})

describe('MediaGrid', () => {
  it('renders images/videos from item.path and skips null paths', () => {
    const { container } = render(
      <MediaGrid
        media={[
          { path: '/media/g/ab/ab1.jpg', type: 'image', url: null },
          { path: null, type: 'image', url: 'https://remote/x.jpg' },
          { path: '/media/g/cd/cd2.mp4', type: 'video', url: null },
        ]}
      />,
    )
    const img = screen.getByRole('img')
    expect(img).toHaveAttribute('src', '/media/g/ab/ab1.jpg')
    const video = container.querySelector('video')
    expect(video?.getAttribute('src')).toBe('/media/g/cd/cd2.mp4')
  })

  it('renders nothing for an empty list', () => {
    const { container } = render(<MediaGrid media={[]} />)
    expect(container).toBeEmptyDOMElement()
  })
})

describe('CommentList', () => {
  it('renders a flat list in input order', () => {
    const comments = [
      makeComment({ id: 'c_1', content_text: '第一条' }),
      makeComment({ id: 'c_2', content_text: '第二条' }),
    ]
    const { container } = render(<CommentList comments={comments} />)
    expect(screen.getByText('第一条')).toBeInTheDocument()
    expect(screen.getByText('第二条')).toBeInTheDocument()
    const items = container.querySelectorAll(':scope > div > div')
    expect(items).toHaveLength(2)
  })

  it('prefers m.path and falls back to m.url for comment media', () => {
    const { container } = render(
      <CommentList
        comments={[
          makeComment({
            id: 'c_1',
            media: [
              { path: '/media/g/ef/ef9.jpg', file: 'ef9.jpg', url: 'https://r/1.jpg', type: 'image', width: 100, height: 100 },
              { path: null, file: null, url: 'https://r/2.jpg', type: 'image', width: 50, height: 50 },
            ],
          }),
        ]}
      />,
    )
    const srcs = Array.from(container.querySelectorAll('img')).map((img) =>
      img.getAttribute('src'),
    )
    expect(srcs).toEqual(['/media/g/ef/ef9.jpg', 'https://r/2.jpg'])
  })

  it('shows the empty state for zero comments', () => {
    render(<CommentList comments={[]} />)
    expect(screen.getByText('暂无评论')).toBeInTheDocument()
  })
})

describe('Layout', () => {
  it('renders the guild selector from the unwrapped envelope (crash regression)', async () => {
    render(
      <QueryClientProvider client={testQueryClient()}>
        <MemoryRouter>
          <GuildProvider>
            <Layout>
              <div>page-content</div>
            </Layout>
          </GuildProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    )
    expect(
      await screen.findByText('7743321643036658 (10091)'),
    ).toBeInTheDocument()
    expect(screen.getByText('page-content')).toBeInTheDocument()
  })

  it('renders the three-key stats in the footer', async () => {
    render(
      <QueryClientProvider client={testQueryClient()}>
        <MemoryRouter>
          <GuildProvider>
            <Layout>
              <div>page-content</div>
            </Layout>
          </GuildProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    )
    await waitFor(() => {
      const footer = screen.getByText(/帖子 .* · 评论 .* · 媒体 /)
      expect(footer.textContent).toContain('10,091')
      expect(footer.textContent).toContain('20,887')
      expect(footer.textContent).toContain('30,112')
    })
  })
})
