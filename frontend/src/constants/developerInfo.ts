/**
 * Developer Profile & Contact Configuration
 *
 * You can modify all your details, social links, skills, and GIF properties
 * in this single file. Any changes here will automatically reflect in the
 * "About Developer" popup inside Settings.
 */

export interface DeveloperSocialLink {
  id: string
  label: string
  url: string
  icon: string
  description?: string
}

export interface DeveloperConfig {
  name: string
  title: string
  tagline: string
  bio: string[]
  location?: string
  skills: string[]
  socialLinks: DeveloperSocialLink[]
  gif: {
    /**
     * Name of the GIF file located in `frontend/public/`
     * E.g. 'developer.gif' corresponds to `frontend/public/developer.gif`
     */
    filename: string
    alt: string
    caption?: string
  }
}

export const DEVELOPER_INFO: DeveloperConfig = {
  name: "Yash",
  title: "Lead AI & Database Systems Developer",
  tagline: "Architecting autonomous agents, intelligent database copilots, and high-performance desktop applications.",
  bio: [
    "Creator and lead developer of QueryPilot — a privacy-first AI database copilot enabling natural language SQL queries with local AST guardrails and multi-database support.",
    "Specializing in full-stack engineering, local LLM orchestration, robust database drivers (SQLite, SQL Server, PostgreSQL, MySQL), and delightful desktop experiences powered by Tauri and React.",
  ],
  location: "India",
  skills: [
    "TypeScript",
    "React",
    "Python",
    "FastAPI",
    "Tauri / Rust",
    "SQL Server",
    "PostgreSQL",
    "MySQL",
    "SQLite",
    "Google Gemini API",
    "SQLGlot / ASTs",
    "Database Engineering",
  ],
  socialLinks: [
    {
      id: "github",
      label: "GitHub",
      url: "https://github.com",
      icon: "🐙",
      description: "Open-source projects & code",
    },
    {
      id: "linkedin",
      label: "LinkedIn",
      url: "https://linkedin.com",
      icon: "💼",
      description: "Professional network & articles",
    },
    {
      id: "portfolio",
      label: "Portfolio",
      url: "https://github.com",
      icon: "🌐",
      description: "Personal website & showcase",
    },
    {
      id: "email",
      label: "Email",
      url: "mailto:developer@querypilot.local",
      icon: "✉️",
      description: "Direct contact & feedback",
    },
  ],
  gif: {
    filename: "developer.gif",
    alt: "Developer coding animation",
    caption: "Crafting QueryPilot with intelligence & craftsmanship ✨",
  },
}
