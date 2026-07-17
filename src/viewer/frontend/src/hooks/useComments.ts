import { useQuery } from '@tanstack/react-query'
import { fetchComments } from '@/lib/api'

export function useComments(feedId: string | undefined) {
  return useQuery({
    queryKey: ['comments', feedId],
    queryFn: () => fetchComments(feedId as string),
    enabled: Boolean(feedId),
  })
}
