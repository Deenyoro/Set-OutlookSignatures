# ================================================================
# KawaSig — Custom Graph API Config for Headless Docker Deployment
# ================================================================
# Reads from environment variables set by Docker .env file.
# Uses client credentials flow (no user interaction needed).
# ================================================================

# --- Authentication ---
$GraphClientID = $env:CLIENT_ID
$GraphClientTenantID = $env:TENANT_ID

$GraphClientSecret = $env:CLIENT_SECRET | ConvertTo-SecureString -AsPlainText -Force
$GraphClientCredential = New-Object System.Management.Automation.PSCredential(
    $GraphClientID,
    $GraphClientSecret
)

# --- Endpoints ---
$GraphEndpointVersion = 'v1.0'
$CloudEnvironmentGraphApiUri = 'https://graph.microsoft.com'
$CloudEnvironmentAzureADAuthority = "https://login.microsoftonline.com/$GraphClientTenantID"

# --- User Attributes ---
$GraphUserProperties = @(
    'businessPhones'
    'city'
    'companyName'
    'country'
    'department'
    'displayName'
    'givenName'
    'id'
    'jobTitle'
    'mail'
    'mailNickname'
    'mobilePhone'
    'officeLocation'
    'onPremisesDistinguishedName'
    'onPremisesExtensionAttributes'
    'onPremisesSamAccountName'
    'postalCode'
    'proxyAddresses'
    'state'
    'streetAddress'
    'surname'
    'usageLocation'
    'userPrincipalName'
)

# --- AD-to-Graph Attribute Mapping ---
$GraphUserAttributeMapping = @{
    'title'                        = 'jobTitle'
    'department'                   = 'department'
    'company'                      = 'companyName'
    'displayName'                  = 'displayName'
    'givenName'                    = 'givenName'
    'sn'                           = 'surname'
    'mail'                         = 'mail'
    'telephoneNumber'              = 'businessPhones[0]'
    'mobile'                       = 'mobilePhone'
    'streetAddress'                = 'streetAddress'
    'l'                            = 'city'
    'st'                           = 'state'
    'postalCode'                   = 'postalCode'
    'co'                           = 'country'
    'physicalDeliveryOfficeName'   = 'officeLocation'
    'extensionAttribute1'          = 'onPremisesExtensionAttributes.extensionAttribute1'
    'extensionAttribute2'          = 'onPremisesExtensionAttributes.extensionAttribute2'
    'extensionAttribute3'          = 'onPremisesExtensionAttributes.extensionAttribute3'
    'extensionAttribute4'          = 'onPremisesExtensionAttributes.extensionAttribute4'
    'extensionAttribute5'          = 'onPremisesExtensionAttributes.extensionAttribute5'
    'extensionAttribute6'          = 'onPremisesExtensionAttributes.extensionAttribute6'
    'extensionAttribute7'          = 'onPremisesExtensionAttributes.extensionAttribute7'
    'extensionAttribute8'          = 'onPremisesExtensionAttributes.extensionAttribute8'
    'extensionAttribute9'          = 'onPremisesExtensionAttributes.extensionAttribute9'
    'extensionAttribute10'         = 'onPremisesExtensionAttributes.extensionAttribute10'
    'extensionAttribute11'         = 'onPremisesExtensionAttributes.extensionAttribute11'
    'extensionAttribute12'         = 'onPremisesExtensionAttributes.extensionAttribute12'
    'extensionAttribute13'         = 'onPremisesExtensionAttributes.extensionAttribute13'
    'extensionAttribute14'         = 'onPremisesExtensionAttributes.extensionAttribute14'
    'extensionAttribute15'         = 'onPremisesExtensionAttributes.extensionAttribute15'
}

# --- Auth Messages (required by script but unused in headless mode) ---
$GraphBrowserAuthWaitMessage = ''
$GraphBrowserAuthSuccessHtml = '<html><body>OK</body></html>'
$GraphBrowserAuthFailHtml = '<html><body>FAIL: {0} - {1}</body></html>'
