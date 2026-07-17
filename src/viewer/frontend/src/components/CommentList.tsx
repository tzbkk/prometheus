import { useState } from 'react'
import type { Comment } from '@/lib/api'

interface CommentListProps {
  comments: Comment[]
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

function CommentItem({
  comment,
  isReply = false,
}: {
  comment: Comment
  isReply?: boolean
}) {
  const [avatarError, setAvatarError] = useState(false)
  const firstLetter = comment.author_nick?.charAt(0) ?? '?'
  const time = formatTime(comment.create_time)
  const showLike = comment.like_count > 0
  const showReply = comment.reply_count > 0
  const showFooter = Boolean(comment.ip_location) || showLike || showReply

  return (
    <div
      className={
        isReply
          ? 'ml-10 rounded-lg bg-gray-50/50 p-3'
          : 'rounded-lg bg-gray-50 p-4'
      }
    >
      <div className="flex items-start gap-3">
        {avatarError || !comment.author_avatar ? (
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-gray-200 text-sm font-medium text-gray-600">
            {firstLetter}
          </div>
        ) : (
          <img
            src={comment.author_avatar}
            alt={comment.author_nick ?? 'avatar'}
            className="h-8 w-8 shrink-0 rounded-full object-cover"
            onError={() => setAvatarError(true)}
          />
        )}
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate text-sm font-medium text-gray-900">
              {comment.author_nick ?? 'Unknown'}
            </span>
            {time && <span className="text-xs text-gray-400">{time}</span>}
          </div>
          {comment.content_text && (
            <p className="mt-1 whitespace-pre-wrap break-words text-sm text-gray-700">
              {comment.content_text}
            </p>
          )}
          {showFooter && (
            <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-gray-400">
              {comment.ip_location && (
                <span>{comment.ip_location}</span>
              )}
              {showLike && <span>♡ {comment.like_count}</span>}
              {showReply && <span>↳ {comment.reply_count}</span>}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

export function CommentList({ comments }: CommentListProps) {
  if (comments.length === 0) {
    return (
      <p className="py-6 text-center text-sm text-gray-400">暂无评论</p>
    )
  }

  const topLevel = comments.filter((c) => c.parent_id === null)
  const repliesByParent = new Map<string, Comment[]>()
  for (const c of comments) {
    if (c.parent_id !== null) {
      const key = c.parent_id
      const list = repliesByParent.get(key) ?? []
      list.push(c)
      repliesByParent.set(key, list)
    }
  }

  return (
    <div className="space-y-3">
      {topLevel.map((comment) => {
        const replies = repliesByParent.get(comment.id) ?? []
        return (
          <div key={comment.id}>
            <CommentItem comment={comment} />
            {replies.length > 0 && (
              <div className="mt-2 space-y-2 border-t border-gray-100 pt-2">
                {replies.map((reply) => (
                  <CommentItem
                    key={reply.id}
                    comment={reply}
                    isReply
                  />
                ))}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

export function CommentListSkeleton() {
  return (
    <div className="space-y-3">
      {[0, 1, 2].map((i) => (
        <div
          key={i}
          className="animate-pulse rounded-lg bg-gray-50 p-4"
        >
          <div className="flex items-start gap-3">
            <div className="h-8 w-8 shrink-0 rounded-full bg-gray-200" />
            <div className="flex-1 space-y-2">
              <div className="h-3 w-24 rounded bg-gray-200" />
              <div className="h-3 w-full rounded bg-gray-200" />
              <div className="h-3 w-2/3 rounded bg-gray-200" />
            </div>
          </div>
        </div>
      ))}
    </div>
  )
}
