// SPDX-License-Identifier: Apache-2.0
// Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
// Play each muted recording while half of it is on the screen, and pause it when it goes away.
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
const videos = document.querySelectorAll("video[muted]");
const observer = new IntersectionObserver(
  (entries) => {
    for (const { target, isIntersecting, intersectionRatio } of entries) {
      // A browser can refuse to play (for example, to save power); the reader then presses Play.
      if (isIntersecting && intersectionRatio >= 0.5) target.play().catch(() => {});
      else target.pause();
    }
  },
  { threshold: 0.5 },
);
const followMotionPreference = () => {
  for (const video of videos) {
    if (reduceMotion.matches) {
      observer.unobserve(video);
      video.pause();
    } else {
      observer.observe(video);
    }
  }
};
followMotionPreference();
reduceMotion.addEventListener("change", followMotionPreference);
