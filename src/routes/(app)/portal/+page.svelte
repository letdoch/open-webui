<script lang="ts">
	import { onMount } from 'svelte';
	import { replaceState } from '$app/navigation';
	import { page } from '$app/stores';
	import { WEBUI_API_BASE_URL } from '$lib/constants';
	import { mobile, showSidebar, WEBUI_NAME } from '$lib/stores';
	import SidebarIcon from '$lib/components/icons/Sidebar.svelte';

	let origin = '';
	let baseUrl = '';
	let configuredHash = '';
	let source = '';
	let currentHash = '';
	let label = 'Portal';
	let error = '';
	let frame: HTMLIFrameElement | undefined;

	/** Build the full portal URL from the validated HTTPS base and optional hash route. */
	function buildPortalUrl(hash: string): string {
		return hash ? `${baseUrl}${hash}` : baseUrl;
	}

	/** Forward only the resolved Open WebUI theme to the configured portal origin. */
	function sendTheme(): void {
		if (origin && frame?.contentWindow) {
			frame.contentWindow.postMessage(
				{
					type: 'owui:theme',
					theme: document.documentElement.classList.contains('dark') ? 'dark' : 'light'
				},
				origin
			);
		}
	}

	/** Sync portal-reported hash routes while rejecting untrusted origins or protocols. */
	function receive(event: MessageEvent): void {
		if (event.origin !== origin || event.source !== frame?.contentWindow) {
			return;
		}
		if (event.data?.type !== 'portal:route' || typeof event.data.hash !== 'string') {
			return;
		}
		if (!event.data.hash.startsWith('#/')) {
			return;
		}
		currentHash = event.data.hash;
		const next = new URL(location.href);
		next.hash = event.data.hash;
		if (next.hash !== location.hash) {
			replaceState(next, $page.state);
		}
	}

	/** Load the configured HTTPS portal endpoint and wire theme and hash synchronization. */
	onMount(() => {
		const controller = new AbortController();
		const observer = new MutationObserver(sendTheme);

		async function load(): Promise<void> {
			try {
				const token = typeof localStorage !== 'undefined' ? (localStorage.token ?? '') : '';
				const response = await fetch(`${WEBUI_API_BASE_URL}/gateway-bridge/portal`, {
					cache: 'no-store',
					credentials: 'same-origin',
					headers: token ? { Authorization: `Bearer ${token}` } : {},
					signal: controller.signal
				});
				if (!response.ok) {
					throw new Error('Portal is not configured.');
				}
				const config: { enabled?: unknown; url?: unknown; label?: unknown } = await response.json();
				if (typeof config.label === 'string' && config.label.trim()) {
					label = config.label.trim();
				}
				if (config.enabled !== true || typeof config.url !== 'string' || !config.url.trim()) {
					throw new Error('Portal is not configured.');
				}
				const target = new URL(config.url);
				const isSelfPortal =
					target.origin === location.origin &&
					(target.pathname === '/portal' || target.pathname.startsWith('/portal/'));
				if (
					target.protocol !== 'https:' ||
					!target.host ||
					target.username ||
					target.password ||
					isSelfPortal
				) {
					throw new Error('Portal requires a valid HTTPS URL.');
				}
				configuredHash = target.hash;
				target.hash = '';
				origin = target.origin;
				baseUrl = target.toString();
				currentHash = location.hash.startsWith('#/') ? location.hash : configuredHash;
				source = buildPortalUrl(currentHash);
			} catch (failure) {
				if (!controller.signal.aborted) {
					error = failure instanceof Error ? failure.message : 'Cannot load portal configuration.';
				}
			}
		}

		function navigate(): void {
			currentHash = location.hash.startsWith('#/') ? location.hash : configuredHash;
			if (baseUrl) {
				source = buildPortalUrl(currentHash);
			}
		}

		window.addEventListener('message', receive);
		window.addEventListener('hashchange', navigate);
		observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
		void load();

		return () => {
			controller.abort();
			observer.disconnect();
			window.removeEventListener('message', receive);
			window.removeEventListener('hashchange', navigate);
		};
	});
</script>

<svelte:head>
	<title>{label} · {$WEBUI_NAME}</title>
</svelte:head>

<div
	class="flex flex-col w-full h-screen max-h-[100dvh] max-w-full {$showSidebar
		? 'md:max-w-[calc(100%-var(--sidebar-width))]'
		: ''}"
>
	<nav
		class="flex items-center gap-2 px-3 py-2 shrink-0 border-b border-gray-100 dark:border-gray-850"
		aria-label="{label} navigation"
	>
		{#if $mobile}
			<button
				type="button"
				aria-label={$showSidebar ? 'Close Sidebar' : 'Open Sidebar'}
				class="p-1 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-850"
				on:click={() => showSidebar.set(!$showSidebar)}
			>
				<SidebarIcon className="size-4" />
			</button>
		{/if}
		<span class="text-sm font-medium">{label}</span>
		{#if baseUrl}
			<a
				class="ml-auto text-xs underline text-gray-600 dark:text-gray-300 hover:text-gray-900 dark:hover:text-white"
				href={buildPortalUrl(currentHash)}
				target="_blank"
				rel="noopener noreferrer"
			>
				Open standalone
			</a>
		{/if}
	</nav>

	{#if error}
		<p role="alert" class="p-4 text-sm text-red-600 dark:text-red-400">{error}</p>
	{:else if source}
		<iframe
			bind:this={frame}
			src={source}
			title={label}
			class="w-full flex-1 min-h-0 border-0"
			allow="clipboard-read; clipboard-write; fullscreen"
			referrerpolicy="strict-origin-when-cross-origin"
			on:load={sendTheme}
		></iframe>
	{:else}
		<p role="status" class="p-4 text-sm text-gray-500 dark:text-gray-400">Loading {label}…</p>
	{/if}
</div>
