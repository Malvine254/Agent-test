# Packaging And Handover Contents

## Teams App Package

The Microsoft Teams app package is built from:

- `appPackage/manifest.json`
- `appPackage/color.png`
- `appPackage/outline.png`
- generated output under `appPackage/build/`

The Teams app package should contain only the Teams manifest and required icon assets. Do not add Word documents or project documentation files into the Teams app zip, because extra files can cause Teams package validation issues.

## Azure App Deployment Package

The Azure App Service deployment package should include the application code and runtime files required to run the bot, including:

- `src/`
- Python dependency files such as `requirements.txt` or `src/requirements.txt`
- infrastructure files when deploying through the Teams Toolkit/Azure workflow
- app configuration through Azure App Service Application Settings

Secrets should be configured through Azure App Service settings or Key Vault references, not bundled into deployment documentation or source control.

## Handover Documentation Package

The following documentation files should be included in customer/tenant handover materials:

- `PROJECT_OVERVIEW_REQUIREMENTS.md`
- `FEATURES_DOCUMENTATION.md`
- `Teams_SharePoint_AI_Assistant_Tenant_Implementation_Guide.docx`
- `Teams_SharePoint_AI_Assistant_Features_Documentation.docx`

These files should be stored in the repository, attached to the project handover package, or uploaded to the customer SharePoint handover location.

## Recommended Handover Folder

Create a handover folder with:

- implementation guide Word document
- features documentation Word document
- project overview and requirements markdown
- deployment notes
- environment variable template without secret values
- testing checklist
- support/runbook notes

## Confirmation

The implementation guide and features documentation are part of the project handover/deployment documentation package. They are not part of the Teams app manifest package.
