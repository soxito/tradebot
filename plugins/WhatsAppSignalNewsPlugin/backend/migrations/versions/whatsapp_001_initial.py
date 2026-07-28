"""WhatsApp Signal & News Plugin - Initial Migration

Revision ID: whatsapp_001
Revises: 
Create Date: 2024-01-01 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ENUM as PGEnum


# revision identifiers, used by Alembic.
revision = 'whatsapp_001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create enum types
    whatsapp_channel_kind = PGEnum(
        'signals', 'news', 'volume_alerts',
        name='whatsapp_channel_kind',
        create_type=True
    )
    whatsapp_source_type = PGEnum(
        'group', 'contact', 'broadcast', 'community',
        name='whatsapp_source_type',
        create_type=True
    )
    signal_status = PGEnum(
        'active', 'filled', 'tp_hit', 'sl_hit', 'closed', 'expired', 'cancelled',
        name='signal_status',
        create_type=True
    )
    sniper_trade_status = PGEnum(
        'pending', 'placed', 'filled', 'skipped', 'failed', 'cancelled',
        name='sniper_trade_status',
        create_type=True
    )
    session_status = PGEnum(
        'disconnected', 'connecting', 'qr_ready', 'authenticated', 'ready', 'failed',
        name='session_status',
        create_type=True
    )

    whatsapp_channel_kind.create(op.get_bind(), checkfirst=True)
    whatsapp_source_type.create(op.get_bind(), checkfirst=True)
    signal_status.create(op.get_bind(), checkfirst=True)
    sniper_trade_status.create(op.get_bind(), checkfirst=True)
    session_status.create(op.get_bind(), checkfirst=True)

    # ────────────────────────────────────────────────────────────────
    # Plugin Settings
    # ────────────────────────────────────────────────────────────────
    op.create_table(
        'whatsapp_plugin_settings',
        sa.Column('id', sa.Integer(), primary_key=True, default=1),
        sa.Column('openwa_base_url', sa.String(500), default='http://localhost:2785'),
        sa.Column('openwa_api_key', sa.String(500), default=''),
        sa.Column('default_session_name', sa.String(100), default='tradebot_whatsapp'),
        sa.Column('webhook_secret', sa.String(500), default=''),
        sa.Column('poll_interval_seconds', sa.Integer(), default=300),
        sa.Column('session_health_check_seconds', sa.Integer(), default=60),
        sa.Column('enable_llm_fallback', sa.Boolean(), default=False),
        sa.Column('llm_model', sa.String(100), default='fable-5-high'),
        sa.Column('llm_timeout_seconds', sa.Integer(), default=20),
        sa.Column('openai_api_key', sa.String(500), default=''),
        sa.Column('max_messages_per_poll', sa.Integer(), default=50),
        sa.Column('message_dedupe_ttl_hours', sa.Integer(), default=24),
        sa.Column('sniper_enabled_default', sa.Boolean(), default=False),
        sa.Column('sniper_mode_default', sa.String(20), default='sandbox'),
        sa.Column('sniper_position_size_usdt_default', sa.Float(), default=100.0),
        sa.Column('sniper_max_positions_default', sa.Integer(), default=5),
        sa.Column('sniper_min_confidence_default', sa.Float(), default=0.65),
        sa.Column('sniper_min_risk_reward_default', sa.Float(), default=1.5),
        sa.Column('label', sa.String(100), nullable=True),
        sa.Column('created_at', sa.DateTime(), default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), default=sa.func.now(), onupdate=sa.func.now()),
    )

    # ────────────────────────────────────────────────────────────────
    # WhatsApp Sessions
    # ────────────────────────────────────────────────────────────────
    op.create_table(
        'whatsapp_sessions',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('session_id', sa.String(100), unique=True, nullable=False, index=True),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('status', sa.String(30), default='disconnected'),
        sa.Column('qr_code', sa.Text(), nullable=True),
        sa.Column('phone_number', sa.String(30), nullable=True),
        sa.Column('platform', sa.String(50), nullable=True),
        sa.Column('battery', sa.Integer(), nullable=True),
        sa.Column('plugged', sa.Boolean(), nullable=True),
        sa.Column('is_default', sa.Boolean(), default=False),
        sa.Column('created_at', sa.DateTime(), default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), default=sa.func.now(), onupdate=sa.func.now()),
        sa.Column('last_connected_at', sa.DateTime(), nullable=True),
    )

    # ────────────────────────────────────────────────────────────────
    # Channel Sources
    # ────────────────────────────────────────────────────────────────
    op.create_table(
        'whatsapp_channel_sources',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), default=0, index=True),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('kind', sa.String(20), default='signals'),
        sa.Column('source_type', sa.String(20), default='group'),
        sa.Column('chat_id', sa.String(100), nullable=False, index=True),
        sa.Column('chat_name', sa.String(200), nullable=True),
        sa.Column('contact_name', sa.String(200), nullable=True),
        sa.Column('phone_number', sa.String(30), nullable=True),
        sa.Column('session_id', sa.String(100), sa.ForeignKey('whatsapp_sessions.session_id', ondelete='CASCADE')),
        sa.Column('enabled', sa.Boolean(), default=True),
        sa.Column('extract_signals', sa.Boolean(), default=True),
        sa.Column('extract_news', sa.Boolean(), default=False),
        sa.Column('use_llm_fallback', sa.Boolean(), default=False),
        sa.Column('llm_model', sa.String(100), nullable=True),
        sa.Column('last_message_id', sa.String(100), nullable=True),
        sa.Column('last_message_timestamp', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), default=sa.func.now(), onupdate=sa.func.now()),
    )
    op.create_index('ix_whatsapp_channel_session_kind', 'whatsapp_channel_sources', ['session_id', 'kind'])
    op.create_unique_constraint('uq_whatsapp_channel_user_chat', 'whatsapp_channel_sources', ['user_id', 'chat_id'])

    # ────────────────────────────────────────────────────────────────
    # Messages
    # ────────────────────────────────────────────────────────────────
    op.create_table(
        'whatsapp_messages',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('channel_source_id', sa.Integer(), sa.ForeignKey('whatsapp_channel_sources.id', ondelete='CASCADE'), index=True),
        sa.Column('session_id', sa.String(100), index=True),
        sa.Column('message_id', sa.String(100), index=True),
        sa.Column('from_me', sa.Boolean(), default=False),
        sa.Column('sender_id', sa.String(100), nullable=True),
        sa.Column('sender_name', sa.String(200), nullable=True),
        sa.Column('text', sa.Text(), nullable=True),
        sa.Column('message_type', sa.String(30), default='text'),
        sa.Column('media_url', sa.String(500), nullable=True),
        sa.Column('media_type', sa.String(30), nullable=True),
        sa.Column('whatsapp_timestamp', sa.DateTime(), index=True),
        sa.Column('received_at', sa.DateTime(), default=sa.func.now()),
        sa.Column('processed', sa.Boolean(), default=False),
        sa.Column('processing_error', sa.Text(), nullable=True),
        sa.Column('raw_payload', sa.JSON(), nullable=True),
    )
    op.create_unique_constraint('uq_whatsapp_message_session_msg', 'whatsapp_messages', ['session_id', 'message_id'])
    op.create_index('ix_whatsapp_message_source_processed', 'whatsapp_messages', ['channel_source_id', 'processed'])

    # ────────────────────────────────────────────────────────────────
    # Parsed Signals
    # ────────────────────────────────────────────────────────────────
    op.create_table(
        'whatsapp_parsed_signals',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('channel_source_id', sa.Integer(), sa.ForeignKey('whatsapp_channel_sources.id', ondelete='CASCADE'), index=True),
        sa.Column('message_id', sa.Integer(), sa.ForeignKey('whatsapp_messages.id', ondelete='CASCADE'), index=True),
        sa.Column('whatsapp_message_id', sa.String(100), index=True),
        sa.Column('symbol', sa.String(30), index=True),
        sa.Column('direction', sa.String(10)),
        sa.Column('market_type', sa.String(10), default='crypto'),
        sa.Column('leverage', sa.Integer(), nullable=True),
        sa.Column('entry', sa.Float(), nullable=True),
        sa.Column('entry_raw', sa.String(100), nullable=True),
        sa.Column('stop_loss', sa.Float(), nullable=True),
        sa.Column('stop_loss_raw', sa.String(100), nullable=True),
        sa.Column('trailing_sl', sa.Float(), nullable=True),
        sa.Column('take_profits', sa.JSON(), default=list),
        sa.Column('tp_reached_count', sa.Integer(), default=0),
        sa.Column('confidence', sa.Float(), default=0.5),
        sa.Column('risk_reward', sa.Float(), nullable=True),
        sa.Column('status', sa.String(20), default='active', index=True),
        sa.Column('raw_text', sa.Text()),
        sa.Column('parsed_at', sa.DateTime(), default=sa.func.now()),
        sa.Column('posted_at', sa.DateTime(), index=True),
        sa.Column('updated_at', sa.DateTime(), default=sa.func.now(), onupdate=sa.func.now()),
        sa.Column('extraction_method', sa.String(30), default='regex'),
        sa.Column('llm_model_used', sa.String(100), nullable=True),
    )
    op.create_index('ix_whatsapp_signal_symbol_status', 'whatsapp_parsed_signals', ['symbol', 'status'])
    op.create_index('ix_whatsapp_signal_posted_at', 'whatsapp_parsed_signals', ['posted_at'])

    # ────────────────────────────────────────────────────────────────
    # Sniper Settings
    # ────────────────────────────────────────────────────────────────
    op.create_table(
        'whatsapp_sniper_settings',
        sa.Column('id', sa.Integer(), primary_key=True, default=1),
        sa.Column('enabled', sa.Boolean(), default=False),
        sa.Column('mode', sa.String(20), default='sandbox'),
        sa.Column('trade_type', sa.String(20), default='market'),
        sa.Column('position_size_usdt', sa.Float(), default=100.0),
        sa.Column('max_positions', sa.Integer(), default=5),
        sa.Column('max_positions_sandbox', sa.Integer(), default=5),
        sa.Column('max_positions_live', sa.Integer(), default=3),
        sa.Column('leverage', sa.Integer(), default=10),
        sa.Column('margin_mode', sa.String(20), default='crossed'),
        sa.Column('sniper_offset_pct', sa.Float(), default=0.5),
        sa.Column('min_confidence', sa.Float(), default=0.65),
        sa.Column('min_risk_reward', sa.Float(), default=1.5),
        sa.Column('pending_ttl_minutes', sa.Integer(), default=30),
        sa.Column('reanalyze', sa.Boolean(), default=True),
        sa.Column('execute_sandbox', sa.Boolean(), default=True),
        sa.Column('execute_live', sa.Boolean(), default=False),
        sa.Column('require_ai_confirmation', sa.Boolean(), default=True),
        sa.Column('execute_immediately', sa.Boolean(), default=True),
        sa.Column('skipped_reanalyze_minutes', sa.Integer(), default=15),
        sa.Column('tp_trail_pct', sa.Float(), default=1.5),
        sa.Column('volume_channel_id', sa.Integer(), nullable=True),
        sa.Column('allowed_channel_ids', sa.JSON(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), default=sa.func.now(), onupdate=sa.func.now()),
    )

    # ────────────────────────────────────────────────────────────────
    # Sniper Trades
    # ────────────────────────────────────────────────────────────────
    op.create_table(
        'whatsapp_sniper_trades',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('signal_id', sa.Integer(), sa.ForeignKey('whatsapp_parsed_signals.id', ondelete='CASCADE'), index=True),
        sa.Column('channel_source_id', sa.Integer(), sa.ForeignKey('whatsapp_channel_sources.id', ondelete='CASCADE'), index=True),
        sa.Column('symbol', sa.String(30), index=True),
        sa.Column('direction', sa.String(10)),
        sa.Column('side', sa.String(10)),
        sa.Column('entry_price', sa.Float(), nullable=True),
        sa.Column('stop_loss', sa.Float(), nullable=True),
        sa.Column('take_profit', sa.Float(), nullable=True),
        sa.Column('leverage', sa.Integer(), default=10),
        sa.Column('margin_mode', sa.String(20), default='crossed'),
        sa.Column('position_size_usdt', sa.Float()),
        sa.Column('quantity', sa.Float(), nullable=True),
        sa.Column('mode', sa.String(20)),
        sa.Column('exchange', sa.String(30), default='bitget'),
        sa.Column('status', sa.String(20), default='pending', index=True),
        sa.Column('order_id', sa.String(100), nullable=True),
        sa.Column('client_order_id', sa.String(100), nullable=True),
        sa.Column('tp_order_ids', sa.JSON(), nullable=True),
        sa.Column('sl_order_id', sa.String(100), nullable=True),
        sa.Column('filled_price', sa.Float(), nullable=True),
        sa.Column('filled_qty', sa.Float(), nullable=True),
        sa.Column('pnl_usdt', sa.Float(), nullable=True),
        sa.Column('pnl_pct', sa.Float(), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), default=sa.func.now(), index=True),
        sa.Column('updated_at', sa.DateTime(), default=sa.func.now(), onupdate=sa.func.now()),
        sa.Column('placed_at', sa.DateTime(), nullable=True),
        sa.Column('filled_at', sa.DateTime(), nullable=True),
        sa.Column('closed_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_whatsapp_sniper_trade_symbol_status', 'whatsapp_sniper_trades', ['symbol', 'status'])
    op.create_index('ix_whatsapp_sniper_trade_created', 'whatsapp_sniper_trades', ['created_at'])

    # ────────────────────────────────────────────────────────────────
    # Channel Presets
    # ────────────────────────────────────────────────────────────────
    op.create_table(
        'whatsapp_channel_presets',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('kind', sa.String(20), default='signals'),
        sa.Column('chat_ids', sa.JSON(), default=list),
        sa.Column('default_config', sa.JSON(), default=dict),
        sa.Column('is_active', sa.Boolean(), default=True),
        sa.Column('created_at', sa.DateTime(), default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), default=sa.func.now(), onupdate=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table('whatsapp_channel_presets')
    op.drop_table('whatsapp_sniper_trades')
    op.drop_table('whatsapp_sniper_settings')
    op.drop_table('whatsapp_parsed_signals')
    op.drop_table('whatsapp_messages')
    op.drop_table('whatsapp_channel_sources')
    op.drop_table('whatsapp_sessions')
    op.drop_table('whatsapp_plugin_settings')

    # Drop enum types
    for enum_name in ['session_status', 'sniper_trade_status', 'signal_status', 'whatsapp_source_type', 'whatsapp_channel_kind']:
        op.execute(f'DROP TYPE IF EXISTS {enum_name}')