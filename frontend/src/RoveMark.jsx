import {useId} from 'react';

export default function RoveMark({size = 18}) {
  const gradId = useId();
  return (
    <svg width={size} height={size} viewBox="0 0 64 64" fill="none" aria-hidden="true">
      <defs>
        <linearGradient id={gradId} x1="18" y1="14" x2="48" y2="50" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stopColor="#ffffff"/>
          <stop offset="42%" stopColor="#f0f9ff"/>
          <stop offset="72%" stopColor="#38bdf8"/>
          <stop offset="100%" stopColor="#818cf8"/>
        </linearGradient>
      </defs>
      <path
        d="M19.5 50V15H33.8a8.8 8.8 0 0 1 0 17.6H19.5"
        stroke={`url(#${gradId})`}
        strokeWidth="6.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M31.5 32.6 45.5 50"
        stroke={`url(#${gradId})`}
        strokeWidth="6.8"
        strokeLinecap="round"
      />
    </svg>
  );
}
