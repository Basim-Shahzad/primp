from .models import Category


def category_tree() -> list[Category]:
    """Active top-level categories with their active children prefetched."""
    return list(
        Category.objects.filter(parent__isnull=True, is_active=True).prefetch_related("children")
    )


def category_ids_with_descendants(category_id: int) -> list[int]:
    ids = [category_id]
    ids += list(Category.objects.filter(parent_id=category_id).values_list("id", flat=True))
    return ids
