import { useState, type FormEvent, type ReactNode } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useGuild } from '@/lib/guild-context'

/**
 * App chrome: sticky header with title + guild selector + a working search
 * bar (input + submit), plus a centered max-width content well for route
 * output. Light theme only (white background, gray text) per the T10 spec;
 * dark mode is explicitly out of scope.
 *
 * Search submit navigates to `/search?q={value}` (URL-encoded) and clears the
 * input field. SearchPage reads the `q` param via useSearchParams and feeds it
 * to useSearch.
 */
export function Layout({ children }: { children: ReactNode }) {
  const navigate = useNavigate()
  const [query, setQuery] = useState('')
  const { selectedGuild, setSelectedGuild, guilds } = useGuild()

  function handleSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const trimmed = query.trim()
    if (!trimmed) return
    navigate(`/search?q=${encodeURIComponent(trimmed)}`)
    setQuery('')
  }

  return (
    <div className="min-h-screen bg-white text-gray-800">
      <header className="sticky top-0 z-10 border-b border-gray-200 bg-white/95 backdrop-blur">
        <div className="mx-auto flex max-w-5xl items-center gap-4 px-4 py-3">
          <Link
            to="/"
            className="text-lg font-semibold text-gray-900 hover:text-gray-700"
          >
            Prometheus Viewer
          </Link>
          <select
            aria-label="选择频道"
            value={selectedGuild ?? ''}
            onChange={(e) => setSelectedGuild(e.target.value || null)}
            className="h-9 max-w-[12rem] truncate rounded border border-gray-300 bg-white px-2 text-sm text-gray-700 outline-none focus:border-gray-400"
          >
            <option value="">全部频道</option>
            {guilds.map((g) => (
              <option key={g.guild_id} value={g.guild_id}>
                {g.name ?? g.guild_id}
              </option>
            ))}
          </select>
          <form
            onSubmit={handleSearch}
            role="search"
            className="ml-auto hidden h-9 items-center sm:flex"
          >
            <input
              type="search"
              name="q"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜索帖子..."
              aria-label="搜索帖子"
              className="h-full w-56 rounded-l border border-r-0 border-gray-300 bg-white px-3 text-sm text-gray-700 outline-none focus:border-gray-400"
            />
            <button
              type="submit"
              aria-label="搜索"
              className="flex h-full items-center rounded-r border border-gray-300 bg-gray-100 px-3 text-sm text-gray-600 hover:bg-gray-200"
            >
              <SearchIcon />
            </button>
          </form>
        </div>
      </header>
      <main className="mx-auto max-w-5xl px-4 py-6">{children}</main>
    </div>
  )
}

function SearchIcon() {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 20 20"
      fill="currentColor"
      className="h-4 w-4"
      aria-hidden="true"
    >
      <path
        fillRule="evenodd"
        d="M9 3.5a5.5 5.5 0 100 11 5.5 5.5 0 000-11zM2 9a7 7 0 1112.452 4.391l3.328 3.329a.75.75 0 11-1.06 1.06l-3.329-3.328A7 7 0 012 9z"
        clipRule="evenodd"
      />
    </svg>
  )
}
