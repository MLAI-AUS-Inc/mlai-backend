"""Explicitly reviewed legacy view classes available to My startup.

Adding a legacy endpoint does not automatically widen the Chat credential scope.
The API aliases keep serializers, permissions, throttles and domain services.
"""

MARKETING_VIEWS = frozenset(
    (
        "ContentIslandResearchAdoptView",
        "ContentIslandResearchView",
        "CustomContentIslandView",
        "EditorialArticlesView",
        "EditorialBriefSuggestionView",
        "EditorialCatalogApprovalView",
        "EditorialCatalogView",
        "VibeMarketingAbnLookupView",
        "VibeMarketingArticleSetupAcceptView",
        "VibeMarketingArticleSetupDisconnectView",
        "VibeMarketingArticleSetupResetView",
        "VibeMarketingArticleSystemRevisionsView",
        "VibeMarketingArticleSystemSetupView",
        "VibeMarketingArticleView",
        "VibeMarketingAutofillView",
        "VibeMarketingBaselineGoogleRefreshView",
        "VibeMarketingBaselineHistoryView",
        "VibeMarketingBaselineSkipView",
        "VibeMarketingBaselineView",
        "VibeMarketingBootstrapView",
        "VibeMarketingCompanyAvatarView",
        "VibeMarketingDailyReplayView",
        "VibeMarketingDiscoveryView",
        "VibeMarketingGitHubConnectView",
        "VibeMarketingGitHubReposView",
        "VibeMarketingLearnedRuleDetailView",
        "VibeMarketingLearnedRulesView",
        "VibeMarketingLocationLookupView",
        "VibeMarketingNotificationChannelDeliveryView",
        "VibeMarketingNotificationChannelDetailView",
        "VibeMarketingNotificationChannelResendView",
        "VibeMarketingNotificationChannelVerifyView",
        "VibeMarketingNotificationChannelsView",
        "VibeMarketingResearchAutomationRunNowView",
        "VibeMarketingResearchAutomationRunStatusView",
        "VibeMarketingResearchAutomationView",
        "VibeMarketingRunArtifactsView",
        "VibeMarketingRunCommentDetailView",
        "VibeMarketingRunCommentsAcceptRevisionView",
        "VibeMarketingRunCommentsSubmitView",
        "VibeMarketingRunCommentsView",
        "VibeMarketingRunControlView",
        "VibeMarketingRunLivePreviewProxyView",
        "VibeMarketingRunLivePreviewResourceView",
        "VibeMarketingRunLivePreviewView",
        "VibeMarketingRunView",
        "VibeMarketingScanView",
        "VibeMarketingSettingsView",
        "VibeMarketingTopicFeedbackRestoreView",
        "VibeMarketingTopicFeedbackView",
        "VibeMarketingWrittenArticleDiscardView",
        "VibeMarketingAnalyticsDisableView",
        "VibeMarketingAnalyticsEnableView",
        "VibeMarketingAnalyticsReportDetailView",
        "VibeMarketingAnalyticsReportsView",
        "VibeMarketingAnalyticsStatusView",
        "VibeMarketingAnalyticsSummaryView",
        "VibeMarketingArticleAnalyticsView",
        "VibeMarketingSearchConsoleVerifyView",
    )
)
FOUNDER_VIEWS = frozenset(
    {
        "FounderToolsBootstrapView",
        "FounderToolsProfileView",
        "FounderToolsCompanyView",
        "FounderToolsCompanyDetailView",
        "FounderToolsActiveCompanyView",
    }
)
POINTS_VIEWS = frozenset(
    {"PointsPacksView", "CurrentUserBalanceView", "CurrentUserPurchaseView"}
)
ACCOUNT_VIEWS = frozenset({"CurrentUserView", "UpdateProfileView"})
LINK_VIEWS = frozenset(
    {
        "SlackFounderLinkStatusView",
        "SlackFounderLinkPreviewView",
        "SlackFounderLinkCompleteView",
    }
)
CONNECTOR_VIEWS = frozenset(
    {
        "ConnectorSourcesStatusView",
        "GoogleAnalyticsPropertyListView",
        "GoogleAnalyticsPropertySelectionView",
        "SlackChannelListView",
        "SlackChannelSelectionView",
    }
)
