<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';
	import { WEBUI_API_BASE_URL } from '$lib/constants';
	import { mobile, showSidebar } from '$lib/stores';
	import Tooltip from '$lib/components/common/Tooltip.svelte';

	export let collapsed = false;

	let enabled = false;
	let label = 'Portal';

	/** Verify that the configured portal URL uses HTTPS without unsafe recursion. */
	function isValidPortalTarget(rawUrl: string): boolean {
		try {
			const target = new URL(rawUrl);
			const isHttps = target.protocol === 'https:';
			const noCredentials = !target.username && !target.password;
			const isSelfPortal =
				target.origin === location.origin &&
				(target.pathname === '/portal' || target.pathname.startsWith('/portal/'));
			return isHttps && Boolean(target.host) && noCredentials && !isSelfPortal;
		} catch {
			return false;
		}
	}

	/** Fetch portal visibility and label from the authenticated gateway bridge endpoint. */
	onMount(() => {
		const controller = new AbortController();

		async function loadPortal(): Promise<void> {
			try {
				const token = typeof localStorage !== 'undefined' ? (localStorage.token ?? '') : '';
				const response = await fetch(`${WEBUI_API_BASE_URL}/gateway-bridge/portal`, {
					cache: 'no-store',
					credentials: 'same-origin',
					headers: token ? { Authorization: `Bearer ${token}` } : {},
					signal: controller.signal
				});
				if (!response.ok) {
					enabled = false;
					return;
				}
				const payload: { enabled?: unknown; url?: unknown; label?: unknown } = await response.json();
				if (payload.enabled !== true || typeof payload.url !== 'string') {
					enabled = false;
					return;
				}
				if (typeof payload.label === 'string' && payload.label.trim()) {
					label = payload.label.trim();
				}
				enabled = isValidPortalTarget(payload.url);
			} catch {
				if (!controller.signal.aborted) {
					enabled = false;
				}
			}
		}

		void loadPortal();
		return () => {
			controller.abort();
		};
	});

	/** Navigate to the embedded portal route without toggling the collapsed sidebar. */
	async function handleClick(event: MouseEvent): Promise<void> {
		if (collapsed) {
			event.stopImmediatePropagation();
			event.stopPropagation();
			event.preventDefault();
			await goto('/portal');
		}
		if ($mobile) {
			showSidebar.set(false);
		}
	}
</script>

{#if enabled}
	{#if collapsed}
		<div>
			<Tooltip content={label} placement="right">
				<a
					class="cursor-pointer flex size-8 items-center justify-center transition group"
					href="/portal"
					draggable="false"
					aria-label={label}
					aria-current={$page.url.pathname === '/portal' ? 'page' : undefined}
					on:click={handleClick}
				>
					<div
						class="self-center flex size-[calc(30px*var(--app-text-scale,1))] items-center justify-center rounded-lg transition {$page
							.url.pathname === '/portal'
							? 'bg-black/[0.035] dark:bg-white/[0.045]'
							: 'group-hover:bg-gray-100 dark:group-hover:bg-gray-900'}"
					>
						<svg
							class="size-4 shrink-0"
							viewBox="0 0 24 24"
							fill="none"
							stroke="currentColor"
							stroke-width="1.5"
							aria-hidden="true"
						>
							<rect x="3" y="3" width="18" height="18" rx="3" />
							<path d="M9 3v18M9 9h12" />
						</svg>
					</div>
				</a>
			</Tooltip>
		</div>
	{:else}
		<div class="px-1 flex justify-center text-gray-700 dark:text-gray-300">
			<a
				id="sidebar-portal-button"
				class="grow flex items-center space-x-2 rounded-xl px-2 py-1.5 transition {$page.url
					.pathname === '/portal'
					? 'bg-black/[0.035] dark:bg-white/[0.045]'
					: 'hover:bg-gray-100 dark:hover:bg-gray-900'}"
				href="/portal"
				draggable="false"
				aria-label={label}
				aria-current={$page.url.pathname === '/portal' ? 'page' : undefined}
				on:click={handleClick}
			>
				<div class="self-center flex size-4 shrink-0 items-center justify-center">
					<svg
						class="size-4 shrink-0"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="1.5"
						aria-hidden="true"
					>
						<rect x="3" y="3" width="18" height="18" rx="3" />
						<path d="M9 3v18M9 9h12" />
					</svg>
				</div>
				<div class="flex self-center translate-y-[0.5px]">
					<div class="self-center text-[0.8125rem] leading-5">{label}</div>
				</div>
			</a>
		</div>
	{/if}
{/if}
