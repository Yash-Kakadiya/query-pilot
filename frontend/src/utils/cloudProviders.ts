import type { ConnectionFailureCode } from '../types/api.ts'

export type CloudProvider =
  | 'monsterasp'
  | 'aws_rds'
  | 'gcp_sql'
  | 'azure_sql'
  | 'supabase'
  | 'neon'
  | 'planetscale'
  | 'custom_cloud'

export interface ProviderStep {
  title: string
  detail: string
  substeps?: string[]
}

export interface FieldGuidance {
  fieldName: string
  howToFind: string
  example: string
  note?: string
}

export interface ProviderGuideInfo {
  name: string
  badge: string
  icon: string
  desc: string
  engineType?: 'mssql' | 'postgresql' | 'mysql'
  defaultPort?: string
  serverPlaceholder: string
  serverHelp: string
  dbPlaceholder: string
  dbHelp: string
  userPlaceholder: string
  userHelp: string
  profileIdPlaceholder: string
  displayNamePlaceholder: string
  fieldGuides: FieldGuidance[]
  steps: ProviderStep[]
  firewallTip?: string
}

export const CLOUD_PROVIDER_CONFIGS: Record<CloudProvider, ProviderGuideInfo> = {
  monsterasp: {
    name: 'MonsterASP.net',
    badge: 'MonsterASP',
    icon: '🌐',
    desc: 'Connect to remote ASP.NET / MonsterASP SQL Server instances. Fast TCP connection with TLS transport encryption.',
    serverPlaceholder: 'e.g. db52589.public.databaseasp.net',
    serverHelp: 'Host from MonsterASP control panel -> MS SQL -> External Server.',
    dbPlaceholder: 'e.g. db52589',
    dbHelp: 'Target remote database name (enter the short DB name prefix, NOT the .databaseasp.net domain).',
    userPlaceholder: 'e.g. db52589',
    userHelp: 'Database username from MonsterASP control panel.',
    profileIdPlaceholder: 'e.g. gradesense_cloud',
    displayNamePlaceholder: 'e.g. GradeSense Cloud DB',
    fieldGuides: [
      {
        fieldName: 'Server Host / Endpoint',
        howToFind: 'Log into monsterasp.net -> click "MS SQL" in the left sidebar -> look under the "External Server" column.',
        example: 'db52589.public.databaseasp.net',
        note: 'Copy just the domain name. Do not include https:// or tcp:.',
      },
      {
        fieldName: 'TCP Port',
        howToFind: 'Standard SQL Server TCP port.',
        example: '1433',
        note: 'Keep the default value 1433.',
      },
      {
        fieldName: 'Database Name',
        howToFind: 'In the MS SQL list, look under the "Database Name" column.',
        example: 'db52589',
        note: 'Important: Enter ONLY the short name (like db52589). Do NOT append .databaseasp.net.',
      },
      {
        fieldName: 'SQL Username',
        howToFind: 'In the MS SQL list, look under the "User Name" column.',
        example: 'db52589',
      },
      {
        fieldName: 'SQL Password',
        howToFind: 'The password you created when adding your database in MonsterASP.',
        example: 'Your database password',
      },
    ],
    steps: [
      {
        title: 'Step 1: Open MonsterASP.net Control Panel',
        detail: 'Log into your monsterasp.net account, click "MS SQL" in the left sidebar.',
      },
      {
        title: 'Step 2: Copy Server Host & Database Name',
        detail: 'Find your database in the list:',
        substeps: [
          'Server Host: Copy the "External Server" address (e.g. db52589.public.databaseasp.net).',
          'Database Name: Copy the short "Database Name" (e.g. db52589). Do not append .databaseasp.net!',
          'Username: Look at "User Name" (e.g. db52589).',
          'Password: Enter the password you chose during database creation.',
        ],
      },
    ],
  },
  aws_rds: {
    name: 'Amazon Web Services (AWS RDS)',
    badge: 'AWS RDS',
    icon: '🟧',
    desc: 'Connect to Amazon RDS for SQL Server (Express, Web, Standard, or Enterprise Edition).',
    serverPlaceholder: 'e.g. database-1.c9xxxxxx.us-east-1.rds.amazonaws.com',
    serverHelp: 'RDS Endpoint from Connectivity & security tab in AWS Console.',
    dbPlaceholder: 'e.g. my_rds_database',
    dbHelp: 'Initial database name or SQL Server database created on the instance.',
    userPlaceholder: 'e.g. admin or db_user',
    userHelp: 'Master username or SQL login created in your RDS instance.',
    profileIdPlaceholder: 'e.g. aws_rds_prod',
    displayNamePlaceholder: 'e.g. AWS RDS Production DB',
    fieldGuides: [
      {
        fieldName: 'Server Host / Endpoint',
        howToFind: 'AWS Console -> RDS -> Databases -> Click your DB instance -> "Connectivity & security" tab -> copy the "Endpoint" string.',
        example: 'database-1.c9xxxxxx.us-east-1.rds.amazonaws.com',
        note: 'If you paste with :1433, QueryPilot will automatically extract the port into the TCP Port field.',
      },
      {
        fieldName: 'TCP Port',
        howToFind: 'Listed next to the Endpoint in the Connectivity & security tab (usually 1433).',
        example: '1433',
      },
      {
        fieldName: 'Database Name',
        howToFind: 'Configuration tab -> "DB name" (or the database you created, e.g. master).',
        example: 'my_rds_database',
      },
      {
        fieldName: 'SQL Username',
        howToFind: 'Configuration tab -> look at "Master username".',
        example: 'admin',
      },
      {
        fieldName: 'SQL Password',
        howToFind: 'The master password you specified when launching the RDS DB instance.',
        example: 'Your RDS master password',
      },
    ],
    steps: [
      {
        title: 'Step 1: Get Endpoint from AWS Console',
        detail: 'Open AWS Console -> Search "RDS" -> Click "Databases" in the left sidebar -> Click your DB instance name.',
      },
      {
        title: 'Step 2: Copy Endpoint & Credentials',
        detail: 'In the "Connectivity & security" tab:',
        substeps: [
          'Endpoint: Copy the full endpoint DNS string (e.g. mydb.c123456789.us-east-1.rds.amazonaws.com) into Server Host.',
          'Port: Verify port is 1433.',
          'Username: In the "Configuration" tab, find your Master username (e.g. admin).',
        ],
      },
      {
        title: 'Step 3: Allow Inbound Connections (VPC Security Group)',
        detail: 'Configure the firewall so your computer can connect:',
        substeps: [
          'Under Connectivity & security, make sure "Publicly Accessible" is set to "Yes".',
          'Click the link under "VPC security groups".',
          'Click the Inbound rules tab -> "Edit inbound rules".',
          'Click "Add rule": Type: MS SQL (port 1433), Source: "My IP" (or 0.0.0.0/0 for testing) -> Save rules.',
        ],
      },
    ],
    firewallTip: '💡 AWS Security Group: If the connection times out, verify your RDS instance is Publicly Accessible and its VPC Security Group allows inbound traffic on port 1433 from your IP.',
  },
  gcp_sql: {
    name: 'Google Cloud SQL (GCP)',
    badge: 'Google Cloud SQL',
    icon: '🟦',
    desc: 'Connect to Google Cloud SQL for SQL Server using Public IP and SQL Server Authentication.',
    serverPlaceholder: 'e.g. 34.123.45.67 (Public IP address)',
    serverHelp: 'Public IP address from Cloud SQL Instance Overview page.',
    dbPlaceholder: 'e.g. my_cloudsql_database',
    dbHelp: 'Name of the database listed in the Cloud SQL "Databases" tab.',
    userPlaceholder: 'e.g. sqlserver or db_user',
    userHelp: 'SQL Server login created under Cloud SQL "Users" tab.',
    profileIdPlaceholder: 'e.g. gcp_sql_prod',
    displayNamePlaceholder: 'e.g. GCP Cloud SQL Instance',
    fieldGuides: [
      {
        fieldName: 'Server Host / Endpoint',
        howToFind: 'Google Cloud Console -> Cloud SQL -> Click your instance -> Overview page -> under "Connect to this instance", copy the "Public IP address".',
        example: '34.123.45.67',
        note: 'Make sure Public IP is enabled under Connections -> Networking.',
      },
      {
        fieldName: 'TCP Port',
        howToFind: 'Standard SQL Server TCP port.',
        example: '1433',
      },
      {
        fieldName: 'Database Name',
        howToFind: 'In the left sidebar of your Cloud SQL instance, click "Databases" -> copy any database name.',
        example: 'my_cloudsql_database',
      },
      {
        fieldName: 'SQL Username',
        howToFind: 'In the left sidebar, click "Users" (default SQL login is sqlserver).',
        example: 'sqlserver',
      },
      {
        fieldName: 'SQL Password',
        howToFind: 'The password set for that user in Cloud SQL (can be reset in the Users tab).',
        example: 'Your Cloud SQL password',
      },
    ],
    steps: [
      {
        title: 'Step 1: Open Google Cloud Console',
        detail: 'Go to console.cloud.google.com -> Cloud SQL -> Click your SQL Server instance.',
      },
      {
        title: 'Step 2: Copy Public IP & Database Name',
        detail: 'On the Overview page:',
        substeps: [
          'Under "Connect to this instance", copy the "Public IP address" into Server Host.',
          'Click "Databases" in the left sidebar to find your database name.',
          'Click "Users" in the left sidebar to see your SQL logins (default is "sqlserver").',
        ],
      },
      {
        title: 'Step 3: Authorize Your Network (Firewall)',
        detail: 'Cloud SQL blocks external IPs by default until authorized:',
        substeps: [
          'In the left menu, click "Connections" -> select the "Networking" tab.',
          'Under "Authorized networks", click "Add network".',
          'Name: "My Desktop", Network: your public IP address (e.g. 103.21.244.0/32) or 0.0.0.0/0 (for testing).',
          'Click "Done" and then "Save" at the bottom of the page.',
        ],
      },
    ],
    firewallTip: '💡 GCP Authorized Networks: Google Cloud SQL will reject connections unless your current public IP is added under Connections -> Networking -> Authorized networks.',
  },
  azure_sql: {
    name: 'Microsoft Azure SQL Database',
    badge: 'Azure SQL',
    icon: '🔷',
    desc: 'Connect to Azure SQL Database or Azure SQL Managed Instance with SQL Authentication.',
    engineType: 'mssql',
    defaultPort: '1433',
    serverPlaceholder: 'e.g. myserver.database.windows.net',
    serverHelp: 'Server name from Azure SQL Database Overview page.',
    dbPlaceholder: 'e.g. my_azure_database',
    dbHelp: 'SQL Database name from Azure Portal.',
    userPlaceholder: 'e.g. serveradmin',
    userHelp: 'Server admin login configured during Azure SQL Server creation.',
    profileIdPlaceholder: 'e.g. azure_sql_prod',
    displayNamePlaceholder: 'e.g. Azure SQL Production',
    fieldGuides: [
      {
        fieldName: 'Server Host / Endpoint',
        howToFind: 'Azure Portal -> SQL databases -> Click your database -> Overview page -> copy the "Server name".',
        example: 'myserver.database.windows.net',
        note: 'Copy the full domain including .database.windows.net.',
      },
      {
        fieldName: 'TCP Port',
        howToFind: 'Standard Azure SQL TCP port.',
        example: '1433',
      },
      {
        fieldName: 'Database Name',
        howToFind: 'The database name displayed at the top of the Overview blade in Azure Portal.',
        example: 'my_azure_database',
      },
      {
        fieldName: 'SQL Username',
        howToFind: 'Overview page -> look at "Server admin login".',
        example: 'serveradmin',
      },
      {
        fieldName: 'SQL Password',
        howToFind: 'The server admin password you configured when creating the Azure SQL server.',
        example: 'Your Azure SQL password',
      },
    ],
    steps: [
      {
        title: 'Step 1: Open Azure Portal',
        detail: 'Navigate to portal.azure.com -> Search "SQL databases" -> Click your database.',
      },
      {
        title: 'Step 2: Copy Server Name & Database Name',
        detail: 'On the Overview blade:',
        substeps: [
          'Server name: Copy the full server name (e.g. myserver.database.windows.net).',
          'Database name: Copy the database name shown at the top.',
          'Username: Look at "Server admin login".',
        ],
      },
      {
        title: 'Step 3: Configure Azure Server Firewall',
        detail: 'Allow your computer to connect through Azure firewall:',
        substeps: [
          'Click "Set server firewall" at the top of the Overview blade (or "Networking" in the left sidebar).',
          'Click "Add your client IPv4 address" (or enter your IP range).',
          'Check "Allow Azure services and resources to access this server" -> Click "Save".',
        ],
      },
    ],
    firewallTip: '💡 Azure Firewall: Make sure to click "Add your client IPv4 address" under Set server firewall, otherwise Azure will block the connection attempt.',
  },
  supabase: {
    name: 'Supabase (PostgreSQL)',
    badge: 'Supabase',
    icon: '⚡',
    desc: 'Connect to your Supabase PostgreSQL project using Session or Transaction mode with SSL.',
    engineType: 'postgresql',
    defaultPort: '5432',
    serverPlaceholder: 'e.g. aws-0-us-east-1.pooler.supabase.com or db.xxxx.supabase.co',
    serverHelp: 'Host from Supabase Dashboard -> Project Settings -> Database -> Connection string.',
    dbPlaceholder: 'postgres',
    dbHelp: 'Target PostgreSQL database name (typically "postgres").',
    userPlaceholder: 'postgres.xxxx (or postgres)',
    userHelp: 'Database username from Supabase connection settings.',
    profileIdPlaceholder: 'e.g. supabase_prod',
    displayNamePlaceholder: 'e.g. Supabase Production DB',
    fieldGuides: [
      {
        fieldName: 'Host / Endpoint',
        howToFind: 'Supabase Dashboard -> Project Settings -> Database -> Connection parameters -> Host.',
        example: 'aws-0-us-east-1.pooler.supabase.com',
      },
      {
        fieldName: 'Port',
        howToFind: 'Port 5432 (Session mode) or 6543 (Transaction pooler mode).',
        example: '5432',
      },
      {
        fieldName: 'Database Name',
        howToFind: 'Default Supabase database is usually postgres.',
        example: 'postgres',
      },
      {
        fieldName: 'Username',
        howToFind: 'User from connection parameters (e.g. postgres.your_project_ref).',
        example: 'postgres.your_project_ref',
      },
    ],
    steps: [
      {
        title: 'Step 1: Get Connection Settings from Supabase',
        detail: 'Log into app.supabase.com -> Select your project -> Project Settings -> Database.',
        substeps: [
          'Under Connection parameters, find Host, Port (5432/6543), Database name, and User.',
          'Copy Host and Port into QueryPilot.',
        ],
      },
      {
        title: 'Step 2: Enter Password & Connect',
        detail: 'Use the database password you set when creating the Supabase project.',
      },
    ],
    firewallTip: '💡 Supabase uses TLS by default. QueryPilot automatically enables SSL mode for secure cloud transit.',
  },
  neon: {
    name: 'Neon Serverless Postgres',
    badge: 'Neon',
    icon: '🟢',
    desc: 'Connect to Neon Serverless PostgreSQL with auto-scaling compute and SSL encryption.',
    engineType: 'postgresql',
    defaultPort: '5432',
    serverPlaceholder: 'e.g. ep-cool-fog-123456.us-east-2.aws.neon.tech',
    serverHelp: 'Host from Neon Console dashboard Connection Details.',
    dbPlaceholder: 'neondb (or postgres)',
    dbHelp: 'Default database name created in Neon.',
    userPlaceholder: 'e.g. alex or neondb_owner',
    userHelp: 'Database user from Neon connection details.',
    profileIdPlaceholder: 'e.g. neon_prod',
    displayNamePlaceholder: 'e.g. Neon Serverless Postgres',
    fieldGuides: [
      {
        fieldName: 'Host / Endpoint',
        howToFind: 'Neon Console -> Dashboard -> Connection Details -> copy Host.',
        example: 'ep-cool-fog-123456.us-east-2.aws.neon.tech',
      },
      {
        fieldName: 'Port',
        howToFind: 'Standard PostgreSQL port 5432.',
        example: '5432',
      },
      {
        fieldName: 'Database Name',
        howToFind: 'Default Neon database is typically neondb.',
        example: 'neondb',
      },
    ],
    steps: [
      {
        title: 'Step 1: Open Neon Console',
        detail: 'Log into console.neon.tech -> Open your project -> Dashboard -> Connection Details widget.',
      },
      {
        title: 'Step 2: Copy Connection Parameters',
        detail: 'Select Parameters or Connection string tab and copy Host, Database, and User.',
      },
    ],
    firewallTip: '💡 Neon databases require SSL. QueryPilot connects using sslmode=require automatically.',
  },
  planetscale: {
    name: 'PlanetScale / Cloud MySQL',
    badge: 'Cloud MySQL',
    icon: '🐬',
    desc: 'Connect to PlanetScale, AWS RDS MySQL, or any remote MySQL / MariaDB database.',
    engineType: 'mysql',
    defaultPort: '3306',
    serverPlaceholder: 'e.g. aws.connect.psdb.cloud or mysql-db.xxxx.rds.amazonaws.com',
    serverHelp: 'Host from your Cloud MySQL provider dashboard.',
    dbPlaceholder: 'e.g. my_mysql_db',
    dbHelp: 'Target MySQL database / schema name.',
    userPlaceholder: 'e.g. root or db_user',
    userHelp: 'MySQL username.',
    profileIdPlaceholder: 'e.g. mysql_prod',
    displayNamePlaceholder: 'e.g. Production MySQL Database',
    fieldGuides: [
      {
        fieldName: 'Server Host / Endpoint',
        howToFind: 'Database overview -> Endpoint / Hostname.',
        example: 'mysql-db.xxxx.rds.amazonaws.com',
      },
      {
        fieldName: 'TCP Port',
        howToFind: 'Standard MySQL TCP port 3306.',
        example: '3306',
      },
      {
        fieldName: 'Database Name',
        howToFind: 'The MySQL database / schema name.',
        example: 'my_mysql_db',
      },
    ],
    steps: [
      {
        title: 'Step 1: Get MySQL Connection Parameters',
        detail: 'Copy Host, Port (3306), Database name, and credentials from your cloud provider console.',
      },
      {
        title: 'Step 2: Firewall & Access Control',
        detail: 'Ensure your MySQL instance allows connections from your IP address or security group.',
      },
    ],
    firewallTip: '💡 MySQL Cloud: Verify that the MySQL user has SELECT privileges and the server firewall allows port 3306.',
  },
  custom_cloud: {
    name: 'Custom Cloud / Remote MSSQL',
    badge: 'Custom Cloud',
    icon: '☁️',
    desc: 'Connect to any remote SQL Server hosted on DigitalOcean, Linode, AWS EC2, or a private VPS.',
    serverPlaceholder: 'e.g. sql.mycompany.com or 198.51.100.25',
    serverHelp: 'Domain name or public IP address of your remote SQL Server.',
    dbPlaceholder: 'e.g. production_db',
    dbHelp: 'Name of the database on your remote SQL Server.',
    userPlaceholder: 'e.g. sql_user',
    userHelp: 'SQL Server Authentication login credentials.',
    profileIdPlaceholder: 'e.g. custom_cloud_db',
    displayNamePlaceholder: 'e.g. Remote Cloud SQL Server',
    fieldGuides: [
      {
        fieldName: 'Server Host / Endpoint',
        howToFind: 'Public IP address or domain name pointing to your remote SQL Server.',
        example: 'sql.mycompany.com or 198.51.100.25',
      },
      {
        fieldName: 'TCP Port',
        howToFind: 'The TCP port SQL Server is configured to listen on (default is 1433).',
        example: '1433',
      },
      {
        fieldName: 'Database Name',
        howToFind: 'The target SQL database name on the remote server.',
        example: 'production_db',
      },
      {
        fieldName: 'SQL Username',
        howToFind: 'SQL Server login user ID created in SQL Server Management Studio (SSMS).',
        example: 'sql_user',
      },
      {
        fieldName: 'SQL Password',
        howToFind: 'Password for the SQL login user.',
        example: 'Your SQL password',
      },
    ],
    steps: [
      {
        title: 'Step 1: Get Server Host & Port',
        detail: 'Enter the public IP or DNS hostname of your remote SQL Server, plus standard TCP port 1433.',
      },
      {
        title: 'Step 2: Verify SQL Authentication & TCP/IP',
        detail: 'In SQL Server Configuration Manager on the host machine:',
        substeps: [
          'Ensure "SQL Server and Windows Authentication mode" (mixed mode) is enabled on the instance.',
          'Verify TCP/IP protocol is enabled in SQL Server Network Configuration.',
          'Ensure the host firewall (Windows Firewall, iptables, or cloud security group) allows inbound TCP port 1433.',
        ],
      },
    ],
  },
}

/**
 * Sanitizes server host inputs by stripping protocol prefixes (http://, https://, tcp:, sqlserver://),
 * trailing path/slashes, and extracting embedded port suffixes (e.g. host:1433 or host,1433).
 */
export function sanitizeServerInput(raw: string): { cleanServer: string; extractedPort?: string } {
  let s = raw.trim()
  // Strip protocol prefix
  s = s.replace(/^(?:https?:\/\/|tcp:|sqlserver:\/\/)/i, '')
  // Strip trailing slashes or URL paths
  s = s.replace(/\/.*$/, '')
  // Detect embedded port after colon or comma (e.g. host.rds.amazonaws.com:1433 or host.database.windows.net,1433)
  const colonMatch = s.match(/:(\d{1,5})$/)
  if (colonMatch && colonMatch.index !== undefined) {
    return { cleanServer: s.slice(0, colonMatch.index), extractedPort: colonMatch[1] }
  }
  const commaMatch = s.match(/,(\d{1,5})$/)
  if (commaMatch && commaMatch.index !== undefined) {
    return { cleanServer: s.slice(0, commaMatch.index), extractedPort: commaMatch[1] }
  }
  return { cleanServer: s }
}

export function mapFailureCodeToFriendlyMessage(
  code?: ConnectionFailureCode | null,
  fallbackMessage?: string,
  isCloud?: boolean,
  provider?: CloudProvider
): string {
  switch (code) {
    case 'AUTH_FAILED':
      return isCloud
        ? 'SQL Server Authentication failed. Please verify your database username and password.'
        : 'Windows Integrated Authentication failed. Please verify that your current Windows user account has access permissions to this SQL Server instance.'
    case 'UNREACHABLE':
      return isCloud
        ? provider === 'aws_rds'
          ? 'AWS RDS instance is unreachable. Ensure "Publicly Accessible" is Yes, and add an Inbound Rule on port 1433 for your IP in your VPC Security Group.'
          : provider === 'gcp_sql'
          ? 'Google Cloud SQL is unreachable. Ensure your current public IP is added under Connections -> Networking -> Authorized networks.'
          : provider === 'azure_sql'
          ? 'Azure SQL Database is unreachable. Ensure your client IP is added under Set server firewall in Azure Portal.'
          : 'Cloud database is unreachable. Please verify the host domain, TCP port (1433), and ensure firewall/security rules allow remote connections.'
        : 'Database instance or file is unreachable. Please verify that the server is running or that the local SQLite database file path is accessible.'
    case 'TIMEOUT':
      return isCloud
        ? 'Connection timed out. Check if your cloud provider firewall or security group allows inbound traffic on port 1433 from your IP address.'
        : 'Connection timed out. The database did not respond within the configured timeout window.'
    case 'DATABASE_MISMATCH':
      return 'Database mismatch. The connected database did not match the expected database identity or canonical file path.'
    case 'DRIVER_ERROR':
      return 'Driver error. Ensure that the required database driver (e.g. ODBC Driver 17 for SQL Server) is installed on this machine.'
    case 'CONNECTION_FAILED':
    default:
      return fallbackMessage || 'Failed to establish connection to the specified database.'
  }
}
