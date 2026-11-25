// Show or hide keys: a bloom's own things, or a leaf inside one of its sprigs.

export const NEST = " › "; // "Sprig › Leaf" in a Show or hide key's Item

/** What a Show or hide key changes: {bloom, item}, or for "Sprig › Leaf" the leaf in its (innermost) sprig. */
export function itemTarget(s) {
	const path = String(s.item ?? "").split(NEST);
	if (path.length > 1) return { sprig: path[path.length - 2], item: path[path.length - 1] };
	return { bloom: s.bloom, item: s.item };
}

