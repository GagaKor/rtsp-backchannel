#!/usr/bin/env node

import { main } from './cli.ts';

main().catch((error) => {
  console.error('error:', error?.message ?? error);
  process.exitCode = 1;
});
