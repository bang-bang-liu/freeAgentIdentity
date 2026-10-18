import { useCallback, useEffect, useState } from 'react'
import ProviderCards from '@/components/settings/ProviderCards'
import { getConfigOptions } from '@/lib/app-data'
import type { ProviderOption, ProviderSetting } from '@/lib/config-options'
import { useI18n } from '@/lib/i18n-context'

export default function Settings() {
  const { t } = useI18n()
  const [catalog, setCatalog] = useState<ProviderOption[]>([])
  const [settings, setSettings] = useState<ProviderSetting[]>([])
  const [error, setError] = useState('')

  const loadMailboxProviders = useCallback(async () => {
    try {
      const options = await getConfigOptions()
      setCatalog(options.mailbox_providers || [])
      setSettings(options.mailbox_settings || [])
      setError('')
    } catch {
      setCatalog([])
      setSettings([])
      setError(t('register.providerMetadataError'))
    }
  }, [t])

  useEffect(() => {
    void loadMailboxProviders()
  }, [loadMailboxProviders])

  return (
    <div className="space-y-4">
      {error && (
        <div className="inline-error" role="alert">
          {error}
        </div>
      )}
      <ProviderCards
        providerType="mailbox"
        catalog={catalog}
        settings={settings}
        onReload={loadMailboxProviders}
      />
    </div>
  )
}
