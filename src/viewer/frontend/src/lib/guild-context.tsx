import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { useQuery } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { fetchGuilds, type Guild } from '@/lib/api'

const GUILD_QUERY_PARAM = 'g'
const STORAGE_KEY = 'prometheus:selectedGuild'

interface GuildContextValue {
  selectedGuild: string | null
  setSelectedGuild: (guildId: string | null) => void
  guilds: Guild[]
  isLoading: boolean
}

const GuildContext = createContext<GuildContextValue | null>(null)

function readStoredGuild(): string | null {
  try {
    const v = localStorage.getItem(STORAGE_KEY)
    return v || null
  } catch {
    return null
  }
}

function writeStoredGuild(guildId: string | null) {
  try {
    if (guildId) localStorage.setItem(STORAGE_KEY, guildId)
    else localStorage.removeItem(STORAGE_KEY)
  } catch {
    // localStorage may be disabled (private mode) — selection stays in-memory.
  }
}

export function GuildProvider({ children }: { children: ReactNode }) {
  const [searchParams, setSearchParams] = useSearchParams()
  const [selectedGuild, setSelectedGuildState] = useState<string | null>(() => {
    const fromUrl = searchParams.get(GUILD_QUERY_PARAM)
    if (fromUrl) {
      // Persist URL-initiated selection so it survives page refreshes after
      // the user navigates away from the URL that carried the param.
      writeStoredGuild(fromUrl)
      return fromUrl
    }
    return readStoredGuild()
  })

  const { data: guilds = [], isLoading } = useQuery({
    queryKey: ['guilds'],
    queryFn: fetchGuilds,
    staleTime: 60 * 1000,
  })

  const setSelectedGuild = useCallback(
    (guildId: string | null) => {
      setSelectedGuildState(guildId)
      writeStoredGuild(guildId)
      setSearchParams(
        (prev) => {
          if (guildId) prev.set(GUILD_QUERY_PARAM, guildId)
          else prev.delete(GUILD_QUERY_PARAM)
          return prev
        },
        { replace: true },
      )
    },
    [setSearchParams],
  )

  // Sync URL → state (inbound only — the URL is a hint, not an authority).
  // Never clear the selection just because the current page omits ?g=;
  // the guild picker must persist across navigations.
  useEffect(() => {
    const fromUrl = searchParams.get(GUILD_QUERY_PARAM)
    if (fromUrl !== null && fromUrl !== selectedGuild) {
      setSelectedGuildState(fromUrl)
      writeStoredGuild(fromUrl)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams])

  const value = useMemo<GuildContextValue>(
    () => ({ selectedGuild, setSelectedGuild, guilds, isLoading }),
    [selectedGuild, setSelectedGuild, guilds, isLoading],
  )

  return <GuildContext.Provider value={value}>{children}</GuildContext.Provider>
}

export function useGuild(): GuildContextValue {
  const ctx = useContext(GuildContext)
  if (!ctx) throw new Error('useGuild must be used inside <GuildProvider>')
  return ctx
}
