@maxLength(20)
@minLength(4)
@description('Used to generate names for all resources in this file')
param resourceBaseName string

@secure()
@description('Required in your bot project to access Azure OpenAI service. You can get it from Azure Portal > OpenAI > Keys > Key1 > Resource Management > Endpoint')  
param azureOpenaiKey string
param azureOpenaiModelDeploymentName string
param azureOpenaiEndpoint string
param azureOpenaiEmbeddingDeployment string = 'text-embedding-3-small'
param embeddingDimensions int = 1536
param appTimezone string = 'UTC'

param webAppSKU string
param linuxFxVersion string

@maxLength(42)
param botDisplayName string

param serverfarmsName string = resourceBaseName
param webAppName string = resourceBaseName
param identityName string = resourceBaseName
param location string = resourceGroup().location
param pythonVersion string = linuxFxVersion

@description('Optional: Sender mailbox UPN for app-only sendMail')
param senderUpn string = ''

@description('Optional: Comma-separated SharePoint site URLs to crawl/search')
param sharepointSites string = ''

@description('Optional: Toggle SharePoint indexing in the deployed environment')
param enableSharepointIndexing bool = true

@description('Optional: Comma-separated external web sources to index')
param externalWebSources string = ''

@description('Azure Cognitive Search endpoint (https://<name>.search.windows.net)')
param azureSearchEndpoint string

@secure()
@description('Azure Cognitive Search admin key')
param azureSearchKey string

@description('Azure Cognitive Search index name')
param azureSearchIndex string

@description('Azure Cognitive Search semantic configuration name')
param azureSearchSemanticConfig string = 'semantic-config'
param azureSearchApiVersion string = '2025-09-01'

@description('Graph Client ID for Microsoft Graph API access')
param graphClientId string = ''

@secure()
@description('Graph Client Secret for Microsoft Graph API access')
param graphClientSecret string = ''

@description('Retry configuration: maximum number of retries')
param retryMaxRetries int = 4

@description('Retry configuration: base delay in seconds')
param retryBaseDelay string = '1.2'

@description('Retry configuration: maximum delay in seconds')
param retryMaxDelay int = 12

@description('Boolean flag: Always call AI Search for every query')
param alwaysCallAiSearch bool = false

// Prompt and runtime tuning parameters (optional)
@description('Limit concurrent OpenAI calls to reduce 429s')
param llmConcurrency int = 1

@description('Minimum cached score before triggering AI Search')
param minCachedScoreBeforeAi int = 55

@description('Minimum cached score before triggering Graph fallback')
param minCachedScoreBeforeGraph int = 30

@description('Max characters per single document included in model input')
param maxDocContextChars int = 3000

@description('Max total characters from documents added to model input')
param maxTotalContextChars int = 12000

@description('Approximate max prompt tokens (fallback limiter)')
param maxPromptTokensApprox int = 3500

@description('Max model completion tokens')
param maxCompletionTokens int = 512

@description('Hard cap for total prompt characters (optional)')
param maxPromptChars int = 14000

@description('Max number of document snippets to include')
param maxDocs int = 3

@description('Max snippet length per document')
param maxSnippetChars int = 800

@description('Max total characters from attachments in prompt')
param maxAttachChars int = 2000

@description('Max total characters from web cache in prompt')
param maxWebChars int = 1200

@description('Max turns of memory to include')
param maxMemoryTurns int = 8

@description('Min interval (seconds) between streaming chunks')
param streamChunkInterval string = '0.3'
param httpTimeout int = 8
param graphTimeout int = 8
param cacheLoadTimeout int = 2
param profileLookupTimeout int = 2
param userDetailsTimeout int = 1
param attachmentCheckTimeout int = 1
param conversationHistoryTimeout int = 1
param sharepointIndexPollSeconds int = 900
param sharepointIndexRunOnStartup bool = true
param sharepointIndexMaxItemsPerRun int = 200
param sharepointIndexMaxDepth int = 8
param minSearchResultsBeforeGraph int = 3
param requireMultiDocumentSearch bool = true
param maxLlmContextChars int = 10000
param maxAttachSnippetChars int = 1000
param maxLlmExposureChars int = 1500
param maxLlmAttachChars int = 12000
param maxFileSizeMb int = 10
param maxContentSizeChars int = 5000000
param maxExtractedChars int = 200000
param maxCacheSizeMb int = 500
param maxMemoryCacheItems int = 100
param maxConversationAttachments int = 8
param allowCacheUserInference bool = false
param disableApisOnAttachments bool = false
param skipSearchForCachedFollowups bool = true
param graphAllowAppOnlyFallback bool = true
param crawlWorkers int = 2
param summaryDocSnippetChars int = 18000
param summaryPrimaryDocChars int = 20000
param summaryTotalContextChars int = 12000

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  location: location
  name: identityName
}

// Compute resources for your Web App
resource serverfarm 'Microsoft.Web/serverfarms@2021-02-01' = {
  kind: 'app,linux'
  location: location
  name: serverfarmsName
  sku: {
    name: webAppSKU
  }
  properties:{
    reserved: true
  }
}

// Web App that hosts your agent
resource webApp 'Microsoft.Web/sites@2021-02-01' = {
  kind: 'app,linux'
  location: location
  name: webAppName
  properties: {
    serverFarmId: serverfarm.id
    siteConfig: {
      alwaysOn: true
      appCommandLine: 'python app.py'
      linuxFxVersion: pythonVersion
      appSettings: [
        {
          name: 'WEBSITES_CONTAINER_START_TIME_LIMIT'
          value: '900'
        }
        {
          name: 'SCM_DO_BUILD_DURING_DEPLOYMENT'
          value: 'true'
        }
        {
          name: 'CLIENT_ID'
          value: identity.properties.clientId
        }
        {
          name: 'AZURE_OPENAI_API_KEY'
          value: azureOpenaiKey
        }
        {
          name: 'AZURE_OPENAI_MODEL_DEPLOYMENT_NAME'
          value: azureOpenaiModelDeploymentName
        }
        {
          name: 'AZURE_OPENAI_ENDPOINT'
          value: azureOpenaiEndpoint
        }
        {
          name: 'AZURE_OPENAI_EMBEDDING_DEPLOYMENT'
          value: azureOpenaiEmbeddingDeployment
        }
        {
          name: 'EMBEDDING_DIMENSIONS'
          value: string(embeddingDimensions)
        }
        {
          name: 'APP_TIMEZONE'
          value: appTimezone
        }
        {
          name: 'TENANT_ID'
          value: identity.properties.tenantId
        }
        { 
          name: 'BOT_TYPE'
          value: 'UserAssignedMsi' 
        }
        {
          name: 'SENDER_UPN'
          value: senderUpn
        }
        {
          name: 'SHAREPOINT_SITES'
          value: sharepointSites
        }
        {
          name: 'ENABLE_SHAREPOINT_INDEXING'
          value: string(enableSharepointIndexing)
        }
        {
          name: 'EXTERNAL_WEB_SOURCES'
          value: externalWebSources
        }
        // Azure Cognitive Search variables
        {
          name: 'AZURE_SEARCH_ENDPOINT'
          value: azureSearchEndpoint
        }
        {
          name: 'AZURE_SEARCH_ADMIN_KEY'
          value: azureSearchKey
        }
        {
          name: 'AZURE_SEARCH_QUERY_KEY'
          value: azureSearchKey
        }
        {
          name: 'AZURE_SEARCH_KEY'
          value: azureSearchKey
        }
        {
          name: 'AZURE_SEARCH_INDEX_NAME'
          value: azureSearchIndex
        }
        {
          name: 'AZURE_SEARCH_INDEX'
          value: azureSearchIndex
        }
        {
          name: 'AZURE_SEARCH_SEMANTIC_CONFIG'
          value: azureSearchSemanticConfig
        }
        {
          name: 'AZURE_SEARCH_API_VERSION'
          value: azureSearchApiVersion
        }
        // Prompt and runtime tunables
        {
          name: 'LLM_CONCURRENCY'
          value: string(llmConcurrency)
        }
        {
          name: 'MIN_CACHED_SCORE_BEFORE_AI'
          value: string(minCachedScoreBeforeAi)
        }
        {
          name: 'MAX_DOC_CONTEXT_CHARS'
          value: string(maxDocContextChars)
        }
        {
          name: 'MAX_TOTAL_CONTEXT_CHARS'
          value: string(maxTotalContextChars)
        }
        {
          name: 'MAX_PROMPT_TOKENS_APPROX'
          value: string(maxPromptTokensApprox)
        }
        {
          name: 'MAX_COMPLETION_TOKENS'
          value: string(maxCompletionTokens)
        }
        {
          name: 'MAX_PROMPT_CHARS'
          value: string(maxPromptChars)
        }
        {
          name: 'MAX_DOCS'
          value: string(maxDocs)
        }
        {
          name: 'MAX_SNIPPET_CHARS'
          value: string(maxSnippetChars)
        }
        {
          name: 'MAX_ATTACH_CHARS'
          value: string(maxAttachChars)
        }
        {
          name: 'MAX_WEB_CHARS'
          value: string(maxWebChars)
        }
        {
          name: 'MAX_MEMORY_TURNS'
          value: string(maxMemoryTurns)
        }
        {
          name: 'STREAM_CHUNK_INTERVAL'
          value: streamChunkInterval
        }
        {
          name: 'HTTP_TIMEOUT'
          value: string(httpTimeout)
        }
        {
          name: 'GRAPH_TIMEOUT'
          value: string(graphTimeout)
        }
        {
          name: 'CACHE_LOAD_TIMEOUT'
          value: string(cacheLoadTimeout)
        }
        {
          name: 'PROFILE_LOOKUP_TIMEOUT'
          value: string(profileLookupTimeout)
        }
        {
          name: 'USER_DETAILS_TIMEOUT'
          value: string(userDetailsTimeout)
        }
        {
          name: 'ATTACHMENT_CHECK_TIMEOUT'
          value: string(attachmentCheckTimeout)
        }
        {
          name: 'CONVERSATION_HISTORY_TIMEOUT'
          value: string(conversationHistoryTimeout)
        }
        {
          name: 'SHAREPOINT_INDEX_RUN_ON_STARTUP'
          value: string(sharepointIndexRunOnStartup)
        }
        {
          name: 'SHAREPOINT_INDEX_POLL_SECONDS'
          value: string(sharepointIndexPollSeconds)
        }
        {
          name: 'SHAREPOINT_INDEX_MAX_ITEMS_PER_RUN'
          value: string(sharepointIndexMaxItemsPerRun)
        }
        {
          name: 'SHAREPOINT_INDEX_MAX_DEPTH'
          value: string(sharepointIndexMaxDepth)
        }
        // Graph API credentials
        {
          name: 'GRAPH_CLIENT_ID'
          value: graphClientId
        }
        {
          name: 'GRAPH_CLIENT_SECRET'
          value: graphClientSecret
        }
        // Retry configuration
        {
          name: 'RETRY_MAX_RETRIES'
          value: string(retryMaxRetries)
        }
        {
          name: 'RETRY_BASE_DELAY'
          value: retryBaseDelay
        }
        {
          name: 'RETRY_MAX_DELAY'
          value: string(retryMaxDelay)
        }
        // Search behavior
        {
          name: 'ALWAYS_CALL_AI_SEARCH'
          value: string(alwaysCallAiSearch)
        }
        {
          name: 'MIN_SEARCH_RESULTS_BEFORE_GRAPH'
          value: string(minSearchResultsBeforeGraph)
        }
        {
          name: 'REQUIRE_MULTI_DOCUMENT_SEARCH'
          value: string(requireMultiDocumentSearch)
        }
        {
          name: 'MIN_CACHED_SCORE_BEFORE_GRAPH'
          value: string(minCachedScoreBeforeGraph)
        }
        {
          name: 'MAX_LLM_CONTEXT_CHARS'
          value: string(maxLlmContextChars)
        }
        {
          name: 'MAX_ATTACH_SNIPPET_CHARS'
          value: string(maxAttachSnippetChars)
        }
        {
          name: 'MAX_LLM_EXPOSURE_CHARS'
          value: string(maxLlmExposureChars)
        }
        {
          name: 'MAX_FILE_SIZE_MB'
          value: string(maxFileSizeMb)
        }
        {
          name: 'MAX_CONTENT_SIZE_CHARS'
          value: string(maxContentSizeChars)
        }
        {
          name: 'MAX_EXTRACTED_CHARS'
          value: string(maxExtractedChars)
        }
        {
          name: 'MAX_CACHE_SIZE_MB'
          value: string(maxCacheSizeMb)
        }
        {
          name: 'MAX_MEMORY_CACHE_ITEMS'
          value: string(maxMemoryCacheItems)
        }
        {
          name: 'MAX_CONVERSATION_ATTACHMENTS'
          value: string(maxConversationAttachments)
        }
        {
          name: 'ALLOW_CACHE_USER_INFERENCE'
          value: string(allowCacheUserInference)
        }
        {
          name: 'DISABLE_APIS_ON_ATTACHMENTS'
          value: string(disableApisOnAttachments)
        }
        {
          name: 'SKIP_SEARCH_FOR_CACHED_FOLLOWUPS'
          value: string(skipSearchForCachedFollowups)
        }
        {
          name: 'GRAPH_ALLOW_APP_ONLY_FALLBACK'
          value: string(graphAllowAppOnlyFallback)
        }
      ]
      ftpsState: 'FtpsOnly'
    }
  }
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
}

// Register your web service as a bot with the Bot Framework
module azureBotRegistration './botRegistration/azurebot.bicep' = {
  name: 'Azure-Bot-registration'
  params: {
    resourceBaseName: resourceBaseName
    identityClientId: identity.properties.clientId
    identityResourceId: identity.id
    identityTenantId: identity.properties.tenantId
    botAppDomain: webApp.properties.defaultHostName
    botDisplayName: botDisplayName
    graphClientId: graphClientId
    graphClientSecret: graphClientSecret
    graphTenantId: !empty(graphClientId) ? (identity.properties.tenantId) : ''
  }
}

// The output will be persisted in .env.{envName}. Visit https://aka.ms/teamsfx-actions/arm-deploy for more details.
output BOT_AZURE_APP_SERVICE_RESOURCE_ID string = webApp.id
output BOT_DOMAIN string = webApp.properties.defaultHostName
output BOT_ENDPOINT string = 'https://${webApp.properties.defaultHostName}'
output BOT_ID string = identity.properties.clientId
output BOT_TENANT_ID string = identity.properties.tenantId
