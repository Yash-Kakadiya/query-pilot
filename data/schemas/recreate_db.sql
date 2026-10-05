-- ============================================================================
-- Safe Database Recreation Script for GradeSense_Local
-- Target Server: localhost\SQLEXPRESS
-- Target Database: GradeSense_Local ONLY
-- Safeguards: Explicitly restricted to GradeSense_Local.
-- ============================================================================

USE [master];
GO

IF DB_ID(N'GradeSense_Local') IS NOT NULL
BEGIN
    PRINT 'Closing connections and dropping staging database [GradeSense_Local]...';
    ALTER DATABASE [GradeSense_Local] SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
    DROP DATABASE [GradeSense_Local];
    PRINT 'Database [GradeSense_Local] dropped successfully.';
END
GO

PRINT 'Creating fresh isolated database [GradeSense_Local]...';
CREATE DATABASE [GradeSense_Local];
PRINT 'Database [GradeSense_Local] created successfully.';
GO
