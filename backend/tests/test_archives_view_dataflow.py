from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_archives_view_lazily_loads_bounded_archive_targets():
    source = (PROJECT_ROOT / "frontend/src/app/views/ArchivesView.tsx").read_text()
    create_block = source[source.index("async function createArchiveFromTarget"):source.index("\n\n  return (")]

    assert "const [archiveRefreshError, setArchiveRefreshError] = React.useState('');" in source
    assert 'message="归档列表刷新失败"' in source
    assert "api<OperationTarget[]>('/operation-targets?target_type=group')" not in source
    assert "import OperationTargetSelect" in source
    assert "createOpen && <OperationTargetSelect" in source
    assert "query={{ targetType: 'group', capability: 'archive' }}" in source
    assert "destroyOnHidden" in source
    assert "setArchiveRefreshError('');" in create_block
    assert "setArchiveRefreshError(error instanceof Error ? error.message : String(error));" in create_block


def test_archive_detail_failure_closes_local_detail_modal():
    view_source = (PROJECT_ROOT / "frontend/src/app/views/ArchivesView.tsx").read_text()
    context_source = (PROJECT_ROOT / "frontend/src/app/context.tsx").read_text()
    context_types_source = (PROJECT_ROOT / "frontend/src/app/context/types.ts").read_text()
    group_view_source = (PROJECT_ROOT / "frontend/src/app/views/GroupManagementView.tsx").read_text()

    open_detail_start = view_source.index("function openDetail")
    open_detail_block = view_source[open_detail_start:view_source.index("\n\n  async function createArchiveFromTarget")]
    context_start = context_source.index("async function openArchiveDetail")
    context_end = context_source.index("\n\n  async function exportArchive", context_start)
    context_block = context_source[context_start:context_end]

    assert "onOpenArchiveDetail: (archive: ArchiveItem) => Promise<boolean>;" in view_source
    assert "onOpenArchiveDetail: (archive: ArchiveItem) => Promise<boolean>;" in group_view_source
    assert "openArchiveDetail: (archive: ArchiveItem) => Promise<boolean>;" in context_types_source

    assert "async function openDetail(archive: ArchiveItem)" in view_source
    assert "const loaded = await onOpenArchiveDetail(archive);" in open_detail_block
    assert "if (!loaded) setDetailArchiveId(null);" in open_detail_block
    assert "void openDetail(archive)" in view_source

    assert "async function openArchiveDetail(archive: ArchiveItem): Promise<boolean>" in context_source
    assert "return true;" in context_block
    assert "return false;" in context_block


def test_archive_detail_requests_are_bound_to_current_archive_and_sequence():
    view_source = (PROJECT_ROOT / "frontend/src/app/views/ArchivesView.tsx").read_text()
    context_source = (PROJECT_ROOT / "frontend/src/app/context.tsx").read_text()

    open_detail_start = view_source.index("async function openDetail")
    open_detail_block = view_source[open_detail_start:view_source.index("\n\n  async function createArchiveFromTarget")]
    modal_start = view_source.index('title={`${detailArchive?.title')
    modal_block = view_source[modal_start:view_source.index("\n      >", modal_start)]
    context_start = context_source.index("async function openArchiveDetail")
    context_end = context_source.index("\n\n  async function exportArchive", context_start)
    context_block = context_source[context_start:context_end]

    assert "const detailRequestSeq = React.useRef(0);" in view_source
    assert "const requestSeq = detailRequestSeq.current + 1;" in open_detail_block
    assert "detailRequestSeq.current = requestSeq;" in open_detail_block
    assert "if (detailRequestSeq.current !== requestSeq) return;" in open_detail_block
    assert "detailRequestSeq.current += 1;" in modal_block

    assert "const archiveDetailRequestRef = React.useRef({ archiveId: null as number | null, seq: 0 });" in context_source
    assert "const archiveId = archive.id;" in context_block
    assert "const requestSeq = archiveDetailRequestRef.current.seq + 1;" in context_block
    assert "archiveDetailRequestRef.current = { archiveId, seq: requestSeq };" in context_block
    assert "archiveDetailRequestRef.current.archiveId !== archiveId || archiveDetailRequestRef.current.seq !== requestSeq" in context_block
    assert "archiveDetailRequestRef.current = { archiveId: null, seq: requestSeq };" in context_block
