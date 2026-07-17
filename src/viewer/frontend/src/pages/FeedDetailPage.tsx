import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useFeedDetail } from '@/hooks/useFeedDetail'
import { useComments } from '@/hooks/useComments'
import { MediaGrid } from '@/components/MediaGrid'
import { CommentList, CommentListSkeleton } from '@/components/CommentList'

interface FeedDetailPageProps {
  id: string
}

function formatTime(unixSeconds: number | null): string {
  if (!unixSeconds) return ''
  return new Date(unixSeconds * 1000).toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function FeedDetailPage({ id }: FeedDetailPageProps) {
  const { data, error, isLoading, refetch } = useFeedDetail(id || undefined)

  if (!id) {
    return <NotFoundState />
  }

  if (isLoading) {
    return <DetailSkeleton />
  }

  if (error) {
    if (error.message.includes('404')) {
      return <NotFoundState id={id} />
    }
    return (
      <div className="rounded-lg border border-gray-200 bg-gray-50 p-8 text-center">
        <p className="text-sm text-gray-500">加载失败</p>
        <p className="mt-1 break-all text-xs text-gray-400">{error.message}</p>
        <button
          onClick={() => refetch()}
          className="mt-4 rounded border border-gray-300 px-4 py-2 text-sm text-gray-700 hover:bg-gray-100"
        >
          重试
        </button>
      </div>
    )
  }

  if (!data) {
    return <NotFoundState id={id} />
  }

  return <FeedDetailContent feed={data} />
}

interface FeedDetailContentProps {
  feed: NonNullable<ReturnType<typeof useFeedDetail>['data']>
}

function FeedDetailContent({ feed }: FeedDetailContentProps) {
  const [avatarError, setAvatarError] = useState(false)
  const firstLetter = feed.author_nick?.charAt(0) ?? '?'
  const media = feed.media ?? []

  return (
    <div className="space-y-4">
      <Link
        to="/"
        className="inline-flex items-center gap-1 text-sm text-gray-500 hover:text-gray-800"
      >
        <span aria-hidden="true">←</span> 返回列表
      </Link>

      <article className="rounded-lg bg-white p-5 shadow-md">
        <div className="flex items-center gap-3">
          {avatarError || !feed.author_avatar ? (
            <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-full bg-gray-200 text-base font-medium text-gray-600">
              {firstLetter}
            </div>
          ) : (
            <img
              src={feed.author_avatar}
              alt={feed.author_nick ?? 'avatar'}
              className="h-12 w-12 shrink-0 rounded-full object-cover"
              onError={() => setAvatarError(true)}
            />
          )}
          <div className="min-w-0 flex-1">
            <p className="truncate text-sm font-medium text-gray-900">
              {feed.author_nick ?? 'Unknown'}
            </p>
            <p className="text-xs text-gray-500">
              {formatTime(feed.create_time)}
            </p>
          </div>
          <div className="flex shrink-0 gap-4 text-xs text-gray-500">
            <span>♡ {feed.like_count}</span>
            <span>💬 {feed.comment_count}</span>
          </div>
        </div>

        {feed.title_text && (
          <p className="mt-4 whitespace-pre-wrap break-words text-base text-gray-800">
            {feed.title_text}
          </p>
        )}

        <MediaGrid media={media} />

        {media.length === 0 && !feed.title_text && (
          <p className="mt-4 text-sm text-gray-400">暂无内容</p>
        )}
      </article>

      <CommentSection feedId={feed.id} />
    </div>
  )
}

function CommentSection({ feedId }: { feedId: string }) {
  const { data, isLoading } = useComments(feedId)
  const count = data?.length ?? 0

  return (
    <section className="mt-6">
      <h2 className="mb-3 flex items-center gap-2 text-base font-medium text-gray-800">
        评论
        {count > 0 && (
          <span className="rounded-full bg-gray-200 px-2 py-0.5 text-xs text-gray-600">
            {count}
          </span>
        )}
      </h2>
      {isLoading ? (
        <CommentListSkeleton />
      ) : !data || data.length === 0 ? (
        <p className="py-6 text-center text-sm text-gray-400">暂无评论</p>
      ) : (
        <CommentList comments={data} />
      )}
    </section>
  )
}

function DetailSkeleton() {
  return (
    <div className="space-y-4">
      <div className="h-4 w-20 animate-pulse rounded bg-gray-200" />
      <div className="animate-pulse rounded-lg bg-white p-5 shadow-md">
        <div className="flex items-center gap-3">
          <div className="h-12 w-12 shrink-0 rounded-full bg-gray-200" />
          <div className="flex-1 space-y-2">
            <div className="h-3 w-32 rounded bg-gray-200" />
            <div className="h-2 w-24 rounded bg-gray-200" />
          </div>
        </div>
        <div className="mt-4 space-y-2">
          <div className="h-4 w-full rounded bg-gray-200" />
          <div className="h-4 w-5/6 rounded bg-gray-200" />
          <div className="h-4 w-2/3 rounded bg-gray-200" />
        </div>
      </div>
    </div>
  )
}

function NotFoundState({ id }: { id?: string }) {
  return (
    <div className="space-y-4">
      <Link
        to="/"
        className="inline-flex items-center gap-1 text-sm text-gray-500 hover:text-gray-800"
      >
        <span aria-hidden="true">←</span> 返回列表
      </Link>
      <div className="rounded-lg border border-gray-200 bg-gray-50 p-12 text-center">
        <p className="text-base font-medium text-gray-700">帖子不存在</p>
        {id && (
          <p className="mt-2 break-all text-xs text-gray-400">ID: {id}</p>
        )}
      </div>
    </div>
  )
}
