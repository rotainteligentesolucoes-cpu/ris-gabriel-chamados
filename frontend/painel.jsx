export default function PainelOperacional() {
    const rotas = [
        {
            id: 1,
            motorista: 'Carlos Silva',
            status: 'Em rota',
            clientes: 18,
            parada: '12:00 - 13:00',
            cor: 'bg-blue-600'
        },
        {
            id: 2,
            motorista: 'Marcos Lima',
            status: 'Aguardando',
            clientes: 12,
            parada: '11:30 - 12:30',
            cor: 'bg-green-600'
        },
        {
            id: 3,
            motorista: 'Fernanda Souza',
            status: 'Finalizada',
            clientes: 24,
            parada: '13:00 - 14:00',
            cor: 'bg-orange-500'
        },
        {
            id: 4,
            motorista: 'Equipe Sul',
            status: 'Em rota',
            clientes: 15,
            parada: '12:15 - 13:15',
            cor: 'bg-purple-600'
        }
    ]

    return (
        <div className="min-h-screen bg-slate-100 p-4">
            <div className="max-w-7xl mx-auto space-y-4">

                <div className="bg-white rounded-3xl shadow-xl p-6 border border-slate-200">
                    <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-4">
                        <div>
                            <h1 className="text-4xl font-bold text-slate-800">
                                Painel Operacional RIS
                            </h1>
                            <p className="text-slate-500 mt-2">
                                Controle inteligente de rotas, entregas e jornadas
                            </p>
                        </div>

                        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                            <div className="bg-slate-100 rounded-2xl p-4 text-center">
                                <div className="text-3xl font-bold text-slate-800">4</div>
                                <div className="text-sm text-slate-500">Rotas</div>
                            </div>

                            <div className="bg-slate-100 rounded-2xl p-4 text-center">
                                <div className="text-3xl font-bold text-slate-800">69</div>
                                <div className="text-sm text-slate-500">Clientes</div>
                            </div>

                            <div className="bg-slate-100 rounded-2xl p-4 text-center">
                                <div className="text-3xl font-bold text-slate-800">3</div>
                                <div className="text-sm text-slate-500">Veículos</div>
                            </div>

                            <div className="bg-slate-100 rounded-2xl p-4 text-center">
                                <div className="text-3xl font-bold text-green-600">92%</div>
                                <div className="text-sm text-slate-500">Eficiência</div>
                            </div>
                        </div>
                    </div>
                </div>

                <div className="grid grid-cols-1 xl:grid-cols-4 gap-4">

                    <div className="xl:col-span-1 bg-white rounded-3xl shadow-xl border border-slate-200 overflow-hidden">
                        <div className="p-5 border-b bg-slate-50">
                            <h2 className="text-2xl font-bold text-slate-800">
                                Rotas do Dia
                            </h2>
                        </div>

                        <div className="divide-y">
                            {rotas.map((rota) => (
                                <div
                                    key={rota.id}
                                    className="p-4 hover:bg-slate-50 transition cursor-pointer"
                                >
                                    <div className="flex items-start justify-between mb-3">
                                        <div>
                                            <div className="flex items-center gap-2">
                                                <div className={`w-4 h-4 rounded-full ${rota.cor}`} />
                                                <h3 className="font-bold text-lg text-slate-800">
                                                    Rota {rota.id}
                                                </h3>
                                            </div>

                                            <p className="text-sm text-slate-500 mt-1">
                                                {rota.motorista}
                                            </p>
                                        </div>

                                        <span className="text-xs bg-slate-100 px-3 py-1 rounded-full font-medium">
                                            {rota.status}
                                        </span>
                                    </div>

                                    <div className="grid grid-cols-2 gap-2 text-sm">
                                        <div className="bg-slate-100 rounded-xl p-2">
                                            <div className="text-slate-500">Clientes</div>
                                            <div className="font-bold text-slate-800">
                                                {rota.clientes}
                                            </div>
                                        </div>

                                        <div className="bg-slate-100 rounded-xl p-2">
                                            <div className="text-slate-500">Intervalo</div>
                                            <div className="font-bold text-slate-800 text-xs">
                                                {rota.parada}
                                            </div>
                                        </div>
                                    </div>
                                </div>
                            ))}
                        </div>
                    </div>

                    <div className="xl:col-span-3 bg-white rounded-3xl shadow-xl border border-slate-200 overflow-hidden">

                        <div className="p-5 border-b bg-slate-50 flex flex-col lg:flex-row lg:items-center lg:justify-between gap-4">
                            <div>
                                <h2 className="text-2xl font-bold text-slate-800">
                                    Mapa Operacional Inteligente
                                </h2>
                                <p className="text-slate-500 text-sm mt-1">
                                    Sequência automática • Rastreamento • ETA • Jornada
                                </p>
                            </div>

                            <div className="flex gap-2 flex-wrap">
                                <button className="bg-blue-600 text-white px-4 py-2 rounded-2xl font-medium hover:opacity-90">
                                    Gerar Rotas
                                </button>

                                <button className="bg-slate-200 px-4 py-2 rounded-2xl font-medium text-slate-700">
                                    Atualizar
                                </button>
                            </div>
                        </div>

                        <div className="relative bg-slate-200 h-[650px] flex items-center justify-center">

                            <div className="absolute top-4 left-4 bg-white rounded-2xl shadow-lg p-3 z-10 w-72">
                                <h3 className="font-bold text-slate-800 mb-3">
                                    Legenda Operacional
                                </h3>

                                <div className="space-y-2 text-sm">
                                    <div className="flex items-center gap-2">
                                        <div className="w-4 h-4 rounded-full bg-blue-600"></div>
                                        <span>Rota 1</span>
                                    </div>

                                    <div className="flex items-center gap-2">
                                        <div className="w-4 h-4 rounded-full bg-green-600"></div>
                                        <span>Rota 2</span>
                                    </div>

                                    <div className="flex items-center gap-2">
                                        <div className="w-4 h-4 rounded-full bg-orange-500"></div>
                                        <span>Rota 3</span>
                                    </div>

                                    <div className="flex items-center gap-2">
                                        <div className="w-4 h-4 rounded-full bg-purple-600"></div>
                                        <span>Rota 4</span>
                                    </div>
                                </div>
                            </div>

                            <div className="absolute bottom-4 left-4 right-4 bg-white rounded-2xl shadow-lg p-4 z-10">
                                <div className="grid grid-cols-2 lg:grid-cols-5 gap-3 text-center">
                                    <div>
                                        <div className="text-sm text-slate-500">Saída Base</div>
                                        <div className="font-bold text-slate-800">07:00</div>
                                    </div>

                                    <div>
                                        <div className="text-sm text-slate-500">Parada Intra</div>
                                        <div className="font-bold text-orange-600">12:00</div>
                                    </div>

                                    <div>
                                        <div className="text-sm text-slate-500">Retorno</div>
                                        <div className="font-bold text-slate-800">18:20</div>
                                    </div>

                                    <div>
                                        <div className="text-sm text-slate-500">Interjornada</div>
                                        <div className="font-bold text-purple-600">11h</div>
                                    </div>

                                    <div>
                                        <div className="text-sm text-slate-500">KM Total</div>
                                        <div className="font-bold text-green-600">246 km</div>
                                    </div>
                                </div>
                            </div>

                            <div className="text-center text-slate-600">
                                <div className="text-3xl font-bold mb-2">
                                    Área do mapa operacional
                                </div>
                                <div>
                                    Aqui entra o Leaflet/Folium com rotas reais pelas ruas
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    )
}
