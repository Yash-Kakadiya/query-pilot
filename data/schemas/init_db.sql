-- ============================================================================
-- QueryPilot Database Initialization Script
-- Target Server: localhost\SQLEXPRESS (or specified development server)
-- Target Database: GradeSense_Local
-- Description: Idempotently creates the isolated GradeSense_Local database.
--              Includes safeguards to guarantee no modification of other databases.
-- ============================================================================

USE [master];
GO

-- Safeguard: Ensure we never touch existing production or prototype databases
IF NOT EXISTS (
    SELECT name 
    FROM sys.databases 
    WHERE name = N'GradeSense_Local'
)
BEGIN
    PRINT 'Creating isolated database [GradeSense_Local]...';
    CREATE DATABASE [GradeSense_Local];
    PRINT 'Database [GradeSense_Local] created successfully.';
END
ELSE
BEGIN
    PRINT 'Database [GradeSense_Local] already exists. Skipping creation.';
END
GO
