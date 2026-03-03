import json


def rustdoc_stable_url(path_segments, kind):
    # Adjust the URL generation based on the method type
    base_url = "https://doc.rust-lang.org/stable/"
    path = '/'.join(path_segments) + (".html#method." if kind == 'method' else ".html#fn.")
    return f"{base_url}{path}"


def collect_unsafe_items(index, paths):
    unsafe_items = []
    # Scan for top-level unsafe functions and traits
    for item_id, item in index.items():
        if item['visibility'] == 'public' and item['header']['is_unsafe']:
            unsafe_items.append(item_id)

    # Second pass for associated items in implementations
    for item_id, item in index.items():
        if 'impl' in item:
            impl_item = item['impl']
            for_type_id = impl_item.get('for')
            type_path = paths[for_type_id]
            type_name = type_path.split('::')[-1]
            module_path = '::'.join(type_path.split('::')[:-1])
            for method_id in impl_item.get('items', []):
                method_item = index[method_id]
                if method_item['visibility'] == 'public' and method_item['header']['is_unsafe']:
                    full_path_segments = type_path.split('::') + [method_item['name']]
                    api_display = f"{type_name}::{method_item['name']}"
                    url = rustdoc_stable_url(full_path_segments, 'method')
                    unsafe_items.append({'kind': 'method', 'url': url, 'api_display': api_display})

    return unsafe_items


def write_html(items):
    for item in items:
        kind = item['kind']
        api_display = item['api_display']
        # Customize rendering based on kind
        print(f"{kind.capitalize()}: {api_display}")

# Example usage
index = { /* your index data */ }
paths = { /* your paths data */ }
unsafe_items = collect_unsafe_items(index, paths)
write_html(unsafe_items)