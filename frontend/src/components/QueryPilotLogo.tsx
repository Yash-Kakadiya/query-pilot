import React from 'react'

interface QueryPilotLogoProps {
  size?: number
  className?: string
  alt?: string
}

export const QueryPilotLogo: React.FC<QueryPilotLogoProps> = ({
  size = 36,
  className = '',
  alt = 'QueryPilot Logo',
}) => {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 512 512"
      width={size}
      height={size}
      className={`querypilot-brand-logo ${className}`.trim()}
      aria-label={alt}
      role="img"
      fill="none"
    >
      <defs>
        <radialGradient id="qlogoBg" cx="50%" cy="46%" r="65%">
          <stop offset="0%" stopColor="#111827" />
          <stop offset="55%" stopColor="#0b1120" />
          <stop offset="100%" stopColor="#060911" />
        </radialGradient>

        <linearGradient id="qlogoRing" x1="110" y1="90" x2="400" y2="400" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stopColor="#00f5ff" />
          <stop offset="25%" stopColor="#38bdf8" />
          <stop offset="65%" stopColor="#6366f1" />
          <stop offset="100%" stopColor="#8b5cf6" />
        </linearGradient>

        <linearGradient id="qlogoVertL" x1="234" y1="256" x2="256" y2="156" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stopColor="#0284c7" />
          <stop offset="60%" stopColor="#38bdf8" />
          <stop offset="100%" stopColor="#ffffff" />
        </linearGradient>

        <linearGradient id="qlogoVertR" x1="278" y1="256" x2="256" y2="156" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stopColor="#4338ca" />
          <stop offset="60%" stopColor="#6366f1" />
          <stop offset="100%" stopColor="#e0e7ff" />
        </linearGradient>

        <linearGradient id="qlogoBladeTopR" x1="256" y1="256" x2="330" y2="182" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stopColor="#f8fafc" />
          <stop offset="100%" stopColor="#ffffff" />
        </linearGradient>
        <linearGradient id="qlogoBladeTopRCyan" x1="256" y1="256" x2="330" y2="182" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stopColor="#0284c7" />
          <stop offset="70%" stopColor="#38bdf8" />
          <stop offset="100%" stopColor="#bae6fd" />
        </linearGradient>

        <linearGradient id="qlogoBladeBotL" x1="256" y1="256" x2="182" y2="330" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stopColor="#0284c7" />
          <stop offset="70%" stopColor="#38bdf8" />
          <stop offset="100%" stopColor="#ffffff" />
        </linearGradient>

        <linearGradient id="qlogoTail" x1="280" y1="310" x2="425" y2="425" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stopColor="#4f46e5" />
          <stop offset="45%" stopColor="#6366f1" />
          <stop offset="80%" stopColor="#38bdf8" />
          <stop offset="100%" stopColor="#00f5ff" />
        </linearGradient>

        <filter id="qlogoGlow" x="-20%" y="-20%" width="140%" height="140%">
          <feGaussianBlur stdDeviation="8" result="blur" />
          <feComposite in="SourceGraphic" in2="blur" operator="over" />
        </filter>

        <filter id="qlogoShadow" x="-25%" y="-25%" width="150%" height="150%">
          <feDropShadow dx="2" dy="5" stdDeviation="6" floodColor="#000000" floodOpacity="0.65" />
        </filter>
      </defs>

      {/* Squircle Canvas Base */}
      <rect x="24" y="24" width="464" height="464" rx="108" fill="url(#qlogoBg)" stroke="#1e293b" strokeWidth="4" />

      {/* Radar Coordinate Geometry */}
      <g opacity="0.38">
        <circle cx="256" cy="256" r="148" stroke="#334155" strokeWidth="1.5" strokeDasharray="4 8" fill="none" />
        <circle cx="256" cy="256" r="108" stroke="#38bdf8" strokeWidth="1.2" strokeDasharray="3 6" fill="none" strokeOpacity="0.6" />
        <circle cx="256" cy="256" r="70" stroke="#334155" strokeWidth="1" strokeDasharray="2 4" fill="none" />
        <line x1="256" y1="96" x2="256" y2="416" stroke="#38bdf8" strokeWidth="1.2" strokeOpacity="0.5" />
        <line x1="96" y1="256" x2="416" y2="256" stroke="#38bdf8" strokeWidth="1.2" strokeOpacity="0.5" />
      </g>

      {/* Cardinal Pips */}
      <line x1="256" y1="64" x2="256" y2="76" stroke="#00f5ff" strokeWidth="3" strokeLinecap="round" />
      <line x1="64" y1="256" x2="76" y2="256" stroke="#38bdf8" strokeWidth="3" strokeLinecap="round" />
      <line x1="256" y1="436" x2="256" y2="448" stroke="#6366f1" strokeWidth="3" strokeLinecap="round" />
      <line x1="436" y1="256" x2="448" y2="256" stroke="#818cf8" strokeWidth="3" strokeLinecap="round" />

      {/* Bold Circular Q Ring */}
      <circle cx="256" cy="256" r="148" stroke="url(#qlogoRing)" strokeWidth="36" fill="none" filter="url(#qlogoGlow)" />

      {/* The Q Tail */}
      <polygon points="274,310 338,300 424,420 376,432 308,356" fill="url(#qlogoTail)" filter="url(#qlogoGlow)" />
      <line x1="338" y1="300" x2="424" y2="420" stroke="#ffffff" strokeWidth="2.5" strokeLinecap="round" strokeOpacity="0.9" />

      {/* Navigation Compass Instrument */}
      <g filter="url(#qlogoShadow)">
        {/* Cross Blade */}
        <polygon points="256,256 330,182 284,236" fill="url(#qlogoBladeTopR)" />
        <polygon points="256,256 330,182 242,216" fill="url(#qlogoBladeTopRCyan)" />
        <line x1="256" y1="256" x2="330" y2="182" stroke="#ffffff" strokeWidth="1.8" strokeLinecap="round" />

        <polygon points="256,256 182,330 228,276" fill="url(#qlogoBladeBotL)" />
        <polygon points="256,256 182,330 270,296" fill="#0f172a" />
        <line x1="256" y1="256" x2="182" y2="330" stroke="#38bdf8" strokeWidth="1.5" strokeLinecap="round" />

        {/* Primary Vertical Needle */}
        <polygon points="256,156 234,256 256,256" fill="url(#qlogoVertL)" />
        <polygon points="256,156 256,256 278,256" fill="url(#qlogoVertR)" />
        <line x1="256" y1="156" x2="256" y2="256" stroke="#ffffff" strokeWidth="2.5" strokeLinecap="round" strokeOpacity="0.95" />

        <polygon points="256,356 234,256 256,256" fill="#0284c7" />
        <polygon points="256,356 256,256 278,256" fill="#4338ca" />
        <line x1="256" y1="356" x2="256" y2="256" stroke="#38bdf8" strokeWidth="2" strokeLinecap="round" strokeOpacity="0.85" />

        {/* Pivot Hub */}
        <circle cx="256" cy="256" r="17" fill="#0b1120" stroke="#00f5ff" strokeWidth="3" />
        <circle cx="256" cy="256" r="9" fill="#1e293b" />
        <circle cx="256" cy="256" r="4.5" fill="#ffffff" />
      </g>

      {/* Beacon Star */}
      <circle cx="256" cy="156" r="5" fill="#ffffff" filter="url(#qlogoGlow)" />
    </svg>
  )
}
