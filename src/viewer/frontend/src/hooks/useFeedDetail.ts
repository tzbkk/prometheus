import { useQuery } from '@tanstack/react-query'
import { fetchFeedDetail } from '@/lib/api'

/**
 * Single-feed detail lookup. Disabled (returns placeholder state) when
 * `feedId` is falsy so callers can mount the hook unconditionally.
 */
export function useFeedDetail(feedId: string | undefined) {
  return useQuery({
    queryKey: ['feed', feedId],
    queryFn: () => fetchFeedDetail(feedId as string),
    enabled: Boolean(feedId),
  })
}
