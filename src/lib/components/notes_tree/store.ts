import { writable } from 'svelte/store';
import type { ReadNodeResponse } from './api';

export const openTreeNoteTabs = writable<ReadNodeResponse[]>([]);
