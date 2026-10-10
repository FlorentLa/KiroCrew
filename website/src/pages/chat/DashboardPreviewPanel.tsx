import { lazy, Suspense } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ExternalLink } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import ErrorBoundary from '../../components/ErrorBoundary'
import ErrorNotice from '../../components/ErrorNotice'
import { Badge } from '../../components/ui'
import { membersRosterQuery } from '../../api/membersQuery'
import { dashboardPreviewUrl } from '../../utils/dashboardPreview'

const CrewDynamicDashboard = lazy(() => import('../members/CrewDynamicDashboard'))

/**
 * A crewmate's STAGED dashboard, in the chat's side panel.
 *
 * The page `dashboard_preview` set aside, rendered by the same frame as the
 * crewmate's Dashboard tab with its `preview` read. Looking at it changes
 * nothing: there is no Apply control here. Keeping the page is the person's
 * answer in the chat, which the agent turns into `dashboard_apply`; this panel
 * only says plainly that what it shows is not applied.
 *
 * The page's own reply options are not wired (`onAct` absent): a reply offered
 * by a page nobody has applied would act on a page that is not the crewmate's.
 */
export default function DashboardPreviewPanel({ slug }: { slug: string }) {
  const { t } = useTranslation()
  // The read is keyed by member NAME as well as slug. The roster names it; a slug
  // two names share is not resolved by guessing, so the frame is withheld.
  const { data: rows, isLoading, isError } = useQuery(membersRosterQuery)
  const matches = (rows ?? []).filter(r => r.slug === slug)
  const member = matches.length === 1 ? matches[0].name : null
  return (
    <div className="h-full min-h-0 flex flex-col" data-testid="dashboard-preview-panel">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 px-3 py-2 border-b border-border">
        <Badge variant="warn" data-testid="dashboard-preview-badge">{t('pages.chat.dashboardPreviewPanel.badge')}</Badge>
        <span className="text-xs text-muted min-w-0 basis-full order-last">{t('pages.chat.dashboardPreviewPanel.hint')}</span>
        <a
          href={dashboardPreviewUrl(slug, member ?? undefined)}
          target="_blank"
          rel="noopener noreferrer"
          className="text-xs text-accent inline-flex items-center gap-1 shrink-0"
          data-testid="dashboard-preview-open-browser"
        >
          <ExternalLink className="lucide-inline" size={12} aria-hidden />
          {t('pages.chat.dashboardPreviewPanel.open_in_browser')}
        </a>
      </div>
      <div className="flex-1 min-h-0 overflow-auto">
        {member ? (
          <ErrorBoundary>
            <Suspense fallback={null}>
              <CrewDynamicDashboard key={slug} slug={slug} member={member} displayName={member} preview />
            </Suspense>
          </ErrorBoundary>
        ) : isLoading ? null : isError ? (
          // A failed roster read is not "no such crewmate". The hand-off is safe
          // here: the panel holds no draft of its own.
          <ErrorNotice className="m-3" askAgent message={t('pages.chat.dashboardPreviewPanel.roster_failed')} testId="dashboard-preview-roster-error" />
        ) : (
          <p className="p-4 text-sm text-muted" data-testid="dashboard-preview-no-member">
            {t('pages.chat.dashboardPreviewPanel.no_member')}
          </p>
        )}
      </div>
    </div>
  )
}
