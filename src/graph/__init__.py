"""App-only Microsoft Graph service layer.

Modules here wrap Microsoft Graph REST endpoints for live productivity features
(mail, calendar, planner, OneDrive/SharePoint files, people lookup) using the
application-permission token from :mod:`sharepoint.graph_client`. Every call that
acts for a specific person targets ``/users/{user_id}`` where ``user_id`` is the
caller's Entra (AAD) object id, captured from the Teams activity.
"""
