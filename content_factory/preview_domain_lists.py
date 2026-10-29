"""Embedded domain lists for preview domain claims (``preview_domain_claim``).

There is no public suffix list or free-mail list in this repository, so these
are hand-kept. Every list only narrows a claim: a missing entry can let one
record cover more than one site, while an extra entry costs at most a template
preview instead of a snapshot. Add to them freely; never remove an entry to
make a claim pass.
"""


def _words(text):
    return frozenset(text.split())


# Names that never resolve to a public website.
RESERVED_TLDS = _words("arpa internal invalid local localhost onion")

# Second-level labels ccTLD registries keep for public registration (com.au,
# co.uk, co.nz, com.sg, ...). Listing too many only narrows a claim.
CC_SECOND_LEVEL = _words("""
    ac act asn co com conf csiro ed edu go gob gouv gov govt gr gv id info lg ltd me mil ne net nom nsw nt or org plc
    qld sa sch tas vic wa""")

# Government and education namespaces have deeper public suffixes (nsw.gov.au,
# k12.ca.us) that cannot be told apart without the full list, so they are never claimed.
UNCLAIMABLE_SECOND_LEVEL = _words("ac ed edu go gob gouv gov govt gv lg mil sch")
UNCLAIMABLE_TLDS = _words("edu gov int mil")

# Regional namespaces registries sell under a ccTLD, so the registrable name is one label deeper.
REGIONAL_SECOND_LEVEL = {
    "ca": _words("ab bc mb nb nf nl ns nt nu on pe qc sk yk"),
}

# Prefecture names under .jp hold local-government and geographic registrations.
JP_PREFECTURES = _words("""
    aichi akita aomori chiba ehime fukui fukuoka fukushima gifu gunma hiroshima hokkaido hyogo ibaraki ishikawa iwate
    kagawa kagoshima kanagawa kochi kumamoto kyoto mie miyagi miyazaki nagano nagasaki nara niigata oita okayama okinawa
    osaka saga saitama shiga shimane shizuoka tochigi tokushima tokyo tottori toyama wakayama yamagata yamaguchi
    yamanashi""")

# Nothing at or under these names belongs to one customer: deep multi-tenant
# cloud roots, tunnels, wildcard DNS, proxies that serve other sites' pages, and
# government namespaces the generic rules miss.
UNCLAIMABLE_ROOTS = _words("""
    amazonaws.com elasticbeanstalk.com cloudfunctions.net run.app r.appspot.com windows.net cloudapp.azure.com
    azurecontainerapps.io azurestaticapps.net googleusercontent.com githubusercontent.com github.dev githubpreview.dev
    gitpod.io stackblitz.io webcontainer.io replit.dev herokudns.com fastly.net akamaized.net edgekey.net
    edgesuite.net secureserver.net
    ngrok.io ngrok-free.app ngrok-free.dev ngrok.app ngrok.dev ngrok.pizza trycloudflare.com cfargotunnel.com loca.lt
    localtunnel.me serveo.net pagekite.me
    nip.io sslip.io xip.io nip.direct localtest.me lvh.me traefik.me
    translate.goog cdn.ampproject.org
    nhs.uk nhs.net police.uk mod.uk parliament.uk gc.ca canada.ca gouv.qc.ca europa.eu bund.de admin.ch nic.in
    school.nz health.nz""") | frozenset(f"{name}.jp" for name in JP_PREFECTURES)

# Hosts where unrelated customers each get a subdomain one label below the root.
SHARED_HOST_SUFFIXES = _words("""
    000webhostapp.com amplifyapp.com appspot.com azureedge.net azurefd.net azurewebsites.net beehiiv.com bigcartel.com
    bitbucket.io blogspot.com bubbleapps.io canva.site cargo.site carrd.co cloudapp.net cloudfront.net cloudwaysapps.com
    codeberg.page company.site csb.app deno.dev edgecompute.app editorx.io firebaseapp.com fly.dev flywheelsites.com
    framer.ai framer.app framer.website ghost.io gitbook.io github.io gitlab.io glitch.me godaddysites.com hashnode.dev
    herokuapp.com hs-sites-eu1.com hs-sites.com hubspotpagebuilder.com jimdo.com jimdofree.com jimdosite.com
    kinsta.cloud koyeb.app livejournal.com lovable.app lovableproject.com mailchimpsites.com medium.com my.canva.site
    mybigcommerce.com myftpupload.com myshopify.com mystrikingly.com neocities.org netlify.app netlify.com notion.site
    now.sh onrender.com pages.dev pantheonsite.io r2.dev railway.app readthedocs.io repl.co replit.app site123.me
    softr.app sourceforge.io square.site squarespace.com strikingly.com substack.com super.site surge.sh tilda.ws
    trafficmanager.net tumblr.com typepad.com up.railway.app vercel.app web.app webflow.io webnode.com webnode.page
    weebly.com weeblysite.com wix.com wixsite.com wixstudio.com wixstudio.io wordpress.com workers.dev
    wpcomstaging.com wpengine.com wpenginepowered.com yolasite.com zohosites.com zohosites.in
    afraid.org ddns.net duckdns.org dynu.net dyndns.org freeddns.org hopto.org mooo.com no-ip.biz no-ip.info no-ip.org
    sytes.net zapto.org
    ae.org africa.com br.com cn.com co.com com.de com.se de.com eu.com eu.org gb.com gb.net hu.com hu.net in.net it.com
    jp.net jpn.com kr.com mex.com no.com pp.ua qc.com ru.com sa.com se.net uk.com uk.net us.com us.org uy.com za.com
    za.net za.org""")

# A platform whose brand sits on many country domains (blogspot.com.au,
# blogspot.co.uk, ...): each customer is one label below whichever it is.
SHARED_HOST_BRANDS = _words("blogspot")

# Mailboxes anyone can open prove nothing about the domain: consumer mail,
# ISPs, privacy and relay services, and disposable inboxes. A listed name also
# covers its subdomains.
FREE_MAIL_DOMAINS = _words("""
    126.com 139.com 163.com 188.com 189.cn 21cn.com aim.com aliyun.com games.com love.com wow.com ygm.com
    googlemail.com gmail.com icloud.com me.com mac.com privaterelay.appleid.com msn.com passport.com
    proton.me proton.ch pm.me hey.com tuta.com tuta.io tutamail.com keemail.me mailbox.org posteo.de posteo.net
    fastmail.com fastmail.fm fastmail.net fastmail.org fastmail.com.au fastmail.co.uk fastmail.us messagingengine.com
    sent.com mail.com email.com usa.com post.com consultant.com engineer.com techie.com writeme.com myself.com
    europe.com asia.com iname.com accountant.com workmail.com cheerful.com dr.com
    hushmail.com hushmail.me hush.com hush.ai mailfence.com runbox.com startmail.com countermail.com disroot.org
    riseup.net autistici.org cock.li kolabnow.com lavabit.com skiff.com onionmail.org ctemplar.com
    duck.com simplelogin.com simplelogin.co simplelogin.io simplelogin.fr slmail.me aleeas.com 8alias.com 8shield.net
    silomails.com slmails.com anonaddy.com anonaddy.me addy.io mozmail.com relay.firefox.com passmail.net passmail.com
    passinbox.com 33mail.com
    verizon.net att.net sbcglobal.net bellsouth.net pacbell.net prodigy.net swbell.net ameritech.net flash.net
    nvbell.net snet.net wans.net comcast.net charter.net spectrum.net cox.net earthlink.net mindspring.com juno.com
    netzero.net netzero.com optonline.net optimum.net frontier.com frontiernet.net windstream.net centurylink.net
    embarqmail.com q.com roadrunner.com rr.com twc.com mediacombb.net suddenlink.net cableone.net hughes.net
    peoplepc.com
    web.de freenet.de arcor.de online.de netcologne.de vodafone.de versanet.de kabelmail.de
    free.fr sfr.fr neuf.fr laposte.net bbox.fr aliceadsl.fr club-internet.fr numericable.fr
    virgilio.it alice.it tin.it fastwebnet.it inwind.it iol.it telefonica.net terra.es terra.com.br ya.com
    wp.pl o2.pl onet.pl onet.eu op.pl interia.pl interia.eu poczta.fm gazeta.pl tlen.pl vp.pl go2.pl
    centrum.cz atlas.cz volny.cz post.cz azet.sk centrum.sk zoznam.sk pobox.sk freemail.hu citromail.hu
    abv.bg dir.bg ziggo.nl kpnmail.nl kpnplanet.nl planet.nl home.nl hetnet.nl chello.nl xs4all.nl telenet.be
    skynet.be proximus.be scarlet.be bluewin.ch sunrise.ch hispeed.ch aon.at chello.at a1.net bredband.net
    comhem.se spray.se online.no broadpark.no frisurf.no start.no jubii.dk ofir.dk stofanet.dk suomi24.fi luukku.com
    kolumbus.fi saunalahti.fi sapo.pt clix.pt netcabo.pt otenet.gr forthnet.gr eircom.net iol.ie
    btinternet.com btopenworld.com btconnect.com talk21.com sky.com virginmedia.com virgin.net blueyonder.co.uk
    ntlworld.com talktalk.net plus.com orange.net freeserve.co.uk lineone.net o2.co.uk tesco.net ukonline.co.uk
    shaw.ca rogers.com sympatico.ca bell.net videotron.ca telus.net cogeco.ca eastlink.ca sasktel.net mts.net
    bigpond.com bigpond.net.au telstra.com iinet.net.au westnet.com.au internode.on.net adam.com.au tpg.com.au
    aapt.net.au iprimus.com.au optusnet.com.au ozemail.com.au dodo.com.au people.net.au netspace.net.au
    chariot.net.au aussiebb.com.au exetel.com.au skymail.com.au activ8.net.au spin.net.au tassie.net.au y7mail.com
    xtra.co.nz slingshot.co.nz orcon.net.nz clear.net.nz paradise.net.nz vodafone.co.nz actrix.co.nz ihug.co.nz
    inspire.net.nz snap.net.nz woosh.co.nz maxnet.co.nz xnet.co.nz
    ya.ru narod.ru list.ru bk.ru internet.ru ro.ru lenta.ru autorambler.ru myrambler.ru ukr.net i.ua meta.ua
    bigmir.net
    daum.net kakao.com nate.com
    qq.com foxmail.com yeah.net sina.com sina.cn sina.com.cn sohu.com tom.com wo.cn
    docomo.ne.jp ezweb.ne.jp au.com softbank.ne.jp i.softbank.jp nifty.com biglobe.ne.jp ocn.ne.jp so-net.ne.jp
    plala.or.jp livedoor.com
    rediff.com sify.com in.com
    uol.com.br bol.com.br ig.com.br globo.com globomail.com oi.com.br r7.com zipmail.com.br prodigy.net.mx
    speedy.com.ar fibertel.com.ar arnet.com.ar latinmail.com starmedia.com
    walla.co.il walla.com netvision.net.il bezeqint.net 012.net.il maktoob.com mweb.co.za telkomsa.net
    webmail.co.za vodamail.co.za iafrica.com
    singnet.com.sg pacific.net.sg tm.net.my streamyx.com hot.ee inbox.lv inbox.lt one.lt
    10mail.org 10minemail.com 10minutemail.co.uk 10minutemail.com 10minutemail.net 1secmail.com 1secmail.net
    1secmail.org 20minutemail.com anonbox.net armyspy.com burnermail.io courriel.fr.nf crazymailing.com cuvox.de
    dayrep.com discard.email dispostable.com dropmail.me einrot.com emailfake.com emailondeck.com
    emailtemporanea.net emltmp.com fakeinbox.com fakemail.net fleckens.hu getairmail.com getnada.com grr.la gustr.com
    harakirimail.com inboxbear.com inboxkitten.com incognitomail.org jetable.fr.nf jourrapide.com linshiyouxiang.net
    mail.gw mail.tm mailcatch.com maildrop.cc mailexpire.com mailforspam.com mailmetrash.com mailnesia.com
    mailnull.com mailpoof.com mailsac.com mailscrap.com mailtemp.info meltmail.com moakt.com mohmal.com
    moncourrier.fr.nf monemail.fr.nf monmail.fr.nf mt2015.com mvrht.com mytemp.email mytrashmail.com nada.email
    no-spam.ws nowmymail.com owlpic.com pokemail.net put2.net rhyta.com sharklasers.com sneakemail.com sofort-mail.de
    spam.la spam4.me spambox.us spamex.com spamfree24.org spamgourmet.com spamhole.com spaml.com spymail.one
    superrito.com teleworm.us tempail.com tempemail.net tempinbox.com tempmail.com tempmail.dev tempmail.net
    tempmail.plus tempmailo.com tempomail.fr temporaryemail.net tempr.email thisisnotmyrealemail.com
    throwawaymail.com tmail.ws tmpmail.net tmpmail.org trash-mail.com trashymail.com wegwerfmail.de wegwerfmail.net
    wh4f.org cool.fr.nf""")

# Mail brands on many country domains (yahoo.co.jp, hotmail.fr, gmx.at, ...):
# a registrable domain whose own label is one of these is a mailbox provider.
FREE_MAIL_BRANDS = _words("""
    aol email excite fastmail gmail gmx googlemail guerrillamail guerrillamailblock hanmail hotmail hushmail icloud
    inbox laposte libero live lycos mail mailinator msn naver orange outlook posteo protonmail rambler rediffmail
    rocketmail seznam t-online temp-mail tiscali trashmail tutanota wanadoo windowslive yahoo yandex ymail yopmail
    zoho zohomail""")
