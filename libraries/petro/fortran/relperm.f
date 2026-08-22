C=======================================================================
C     RELPERM  -   RELATIVE PERMEABILITY AND CAPILLARY PRESSURE
C
C     WRITTEN   27-NOV-1988   M. OKONKWO
C     REVISED   03-JUN-1990   STONE II REPLACES STONE I AS DEFAULT
C     REVISED   14-AUG-1994   TABLE LOOKUP (SWOF/SGOF CARDS) ADDED
C
C     TWO MODES OF OPERATION, SELECTED BY ITABLE IN /KRSEL/
C         ITABLE = 0   ANALYTIC COREY CURVES FROM END POINTS
C         ITABLE = 1   PIECEWISE LINEAR INTERPOLATION IN SWOF/SGOF
C
C     THE TABLE ARRAYS ARE LOADED BY KRLOAD, WHICH IS CALLED FROM THE
C     DECK READER (SEE ../cpp/src/DeckReader.cpp).  DO NOT CALL THE
C     LOOKUP ROUTINES BEFORE KRLOAD OR YOU WILL INTERPOLATE GARBAGE -
C     THERE IS NO INITIALISATION FLAG, BY DESIGN, BECAUSE THE 1988
C     VERSION RAN OUT OF BLANK COMMON.
C=======================================================================
      SUBROUTINE KRINI (SWC, SOR, SGC, EW, EO, EG, KRWM, KROM, KRGM)
C-----------------------------------------------------------------------
C     SET COREY END POINTS AND EXPONENTS.  SWITCHES TO ANALYTIC MODE.
C-----------------------------------------------------------------------
      INCLUDE 'PETRO.INC'
      COMMON /KREND/ SWCON, SORW, SORG, SGCON, EXPW, EXPO, EXPG,
     &               KRWMAX, KROMAX, KRGMAX, PCWMAX, PCGMAX
      COMMON /KRSEL/ ITABLE, NSWOF, NSGOF, ISTONE
      DOUBLE PRECISION KRWMAX, KROMAX, KRGMAX
      DOUBLE PRECISION KRWM, KROM, KRGM
C
      SWCON  = SWC
      SORW   = SOR
      SORG   = SOR
      SGCON  = SGC
      EXPW   = EW
      EXPO   = EO
      EXPG   = EG
      KRWMAX = KRWM
      KROMAX = KROM
      KRGMAX = KRGM
      PCWMAX = 12.0D0
      PCGMAX = 4.0D0
      ITABLE = 0
      ISTONE = 2
C
      IF (SWCON + SORW .GE. ONE) THEN
         IERR = 11
         IF (LUNPRT .GT. 0) WRITE (LUNPRT,9100) SWCON, SORW
      END IF
      RETURN
 9100 FORMAT (' *** KRINI - SWCON + SORW =',2F8.4,' EXCEEDS UNITY')
      END
C
C-----------------------------------------------------------------------
      SUBROUTINE KRLOAD (SWTAB, KRWTAB, KROTAB, PCWTAB, N)
C-----------------------------------------------------------------------
C     LOAD AN SWOF TABLE.  N ROWS, ASCENDING IN SW.  SWITCHES TO
C     TABLE MODE.  CALLED FROM THE C++ DECK READER VIA KRLOAD_.
C-----------------------------------------------------------------------
      INCLUDE 'PETRO.INC'
      DIMENSION SWTAB(N), KRWTAB(N), KROTAB(N), PCWTAB(N)
      DOUBLE PRECISION KRWTAB, KROTAB
      DOUBLE PRECISION TLOOK
      COMMON /KRTAB/ TSW(NTABMX), TKRW(NTABMX), TKRO(NTABMX),
     &               TPCW(NTABMX),
     &               TSG(NTABMX), TKRG(NTABMX), TKROG(NTABMX),
     &               TPCG(NTABMX)
      COMMON /KRSEL/ ITABLE, NSWOF, NSGOF, ISTONE
C
      IF (N .GT. NTABMX) THEN
         IERR = 12
         RETURN
      END IF
      DO 100 I = 1, N
         IF (I .GT. 1 .AND. SWTAB(I) .LE. SWTAB(MAX(I-1,1))) THEN
            IERR = 13
            RETURN
         END IF
         TSW (I) = SWTAB (I)
         TKRW(I) = KRWTAB(I)
         TKRO(I) = KROTAB(I)
         TPCW(I) = PCWTAB(I)
  100 CONTINUE
      NSWOF  = N
      ITABLE = 1
      RETURN
      END
C
C-----------------------------------------------------------------------
      DOUBLE PRECISION FUNCTION KRWAT (SW)
C-----------------------------------------------------------------------
C     WATER RELATIVE PERMEABILITY.
C-----------------------------------------------------------------------
      INCLUDE 'PETRO.INC'
      COMMON /KREND/ SWCON, SORW, SORG, SGCON, EXPW, EXPO, EXPG,
     &               KRWMAX, KROMAX, KRGMAX, PCWMAX, PCGMAX
      COMMON /KRTAB/ TSW(NTABMX), TKRW(NTABMX), TKRO(NTABMX),
     &               TPCW(NTABMX),
     &               TSG(NTABMX), TKRG(NTABMX), TKROG(NTABMX),
     &               TPCG(NTABMX)
      COMMON /KRSEL/ ITABLE, NSWOF, NSGOF, ISTONE
      DOUBLE PRECISION KRWMAX, KROMAX, KRGMAX
C
      IF (ITABLE .EQ. 1) THEN
         KRWAT = TLOOK(TSW, TKRW, NSWOF, SW)
         RETURN
      END IF
C
      DEN = ONE - SWCON - SORW
      IF (DEN .LE. SMALL) THEN
         KRWAT = ZERO
         RETURN
      END IF
      SN = (SW - SWCON) / DEN
      IF (SN .LE. ZERO) THEN
         KRWAT = ZERO
      ELSE IF (SN .GE. ONE) THEN
         KRWAT = KRWMAX
      ELSE
         KRWAT = KRWMAX * SN**EXPW
      END IF
      RETURN
      END
C
C-----------------------------------------------------------------------
      DOUBLE PRECISION FUNCTION KROW (SW)
C-----------------------------------------------------------------------
C     OIL RELATIVE PERMEABILITY IN THE OIL-WATER SYSTEM.
C-----------------------------------------------------------------------
      INCLUDE 'PETRO.INC'
      COMMON /KREND/ SWCON, SORW, SORG, SGCON, EXPW, EXPO, EXPG,
     &               KRWMAX, KROMAX, KRGMAX, PCWMAX, PCGMAX
      COMMON /KRTAB/ TSW(NTABMX), TKRW(NTABMX), TKRO(NTABMX),
     &               TPCW(NTABMX),
     &               TSG(NTABMX), TKRG(NTABMX), TKROG(NTABMX),
     &               TPCG(NTABMX)
      COMMON /KRSEL/ ITABLE, NSWOF, NSGOF, ISTONE
      DOUBLE PRECISION KRWMAX, KROMAX, KRGMAX
C
      IF (ITABLE .EQ. 1) THEN
         KROW = TLOOK(TSW, TKRO, NSWOF, SW)
         RETURN
      END IF
C
      DEN = ONE - SWCON - SORW
      IF (DEN .LE. SMALL) THEN
         KROW = ZERO
         RETURN
      END IF
      SN = (ONE - SW - SORW) / DEN
      IF (SN .LE. ZERO) THEN
         KROW = ZERO
      ELSE IF (SN .GE. ONE) THEN
         KROW = KROMAX
      ELSE
         KROW = KROMAX * SN**EXPO
      END IF
      RETURN
      END
C
C-----------------------------------------------------------------------
      DOUBLE PRECISION FUNCTION KRGAS (SG)
C-----------------------------------------------------------------------
C     GAS RELATIVE PERMEABILITY.
C-----------------------------------------------------------------------
      INCLUDE 'PETRO.INC'
      COMMON /KREND/ SWCON, SORW, SORG, SGCON, EXPW, EXPO, EXPG,
     &               KRWMAX, KROMAX, KRGMAX, PCWMAX, PCGMAX
      DOUBLE PRECISION KRWMAX, KROMAX, KRGMAX
C
      DEN = ONE - SWCON - SGCON - SORG
      IF (DEN .LE. SMALL) THEN
         KRGAS = ZERO
         RETURN
      END IF
      SN = (SG - SGCON) / DEN
      IF (SN .LE. ZERO) THEN
         KRGAS = ZERO
      ELSE IF (SN .GE. ONE) THEN
         KRGAS = KRGMAX
      ELSE
         KRGAS = KRGMAX * SN**EXPG
      END IF
      RETURN
      END
C
C-----------------------------------------------------------------------
      DOUBLE PRECISION FUNCTION KROIL (SW, SG)
C-----------------------------------------------------------------------
C     THREE PHASE OIL RELATIVE PERMEABILITY.
C     ISTONE = 1  STONE I (NORMALISED)
C     ISTONE = 2  STONE II
C     ISTONE = 3  SATURATION WEIGHTED (BAKER)
C-----------------------------------------------------------------------
      INCLUDE 'PETRO.INC'
      COMMON /KREND/ SWCON, SORW, SORG, SGCON, EXPW, EXPO, EXPG,
     &               KRWMAX, KROMAX, KRGMAX, PCWMAX, PCGMAX
      COMMON /KRSEL/ ITABLE, NSWOF, NSGOF, ISTONE
      DOUBLE PRECISION KRWMAX, KROMAX, KRGMAX
      DOUBLE PRECISION KRW, KRG, KOW, KOG, KROCW
      DOUBLE PRECISION KRWAT, KRGAS, KROW, KROGF
C
      KROCW = KROMAX
      KRW   = KRWAT(SW)
      KRG   = KRGAS(SG)
      KOW   = KROW (SW)
      KOG   = KROGF(SG)
C
      IF (KROCW .LE. SMALL) THEN
         KROIL = ZERO
         RETURN
      END IF
C
      GO TO (410, 420, 430), ISTONE
C
  410 CONTINUE
C     ---- STONE I
      SO   = ONE - SW - SG
      SOM  = MIN(SORW, SORG)
      DEN  = ONE - SWCON - SOM
      IF (DEN .LE. SMALL) GO TO 420
      SOS  = (SO - SOM) / DEN
      SWS  = (SW - SWCON) / DEN
      SGS  = SG / DEN
      IF (SOS .LE. ZERO) THEN
         KROIL = ZERO
      ELSE
         B1 = ONE - SWS
         B2 = ONE - SGS
         IF (B1 .LE. SMALL .OR. B2 .LE. SMALL) THEN
            KROIL = ZERO
         ELSE
            KROIL = SOS * (KOW/(KROCW*B1)) * (KOG/(KROCW*B2)) * KROCW
         END IF
      END IF
      GO TO 490
C
  420 CONTINUE
C     ---- STONE II
      KROIL = KROCW * ((KOW/KROCW + KRW) * (KOG/KROCW + KRG)
     &                 - KRW - KRG)
      GO TO 490
C
  430 CONTINUE
C     ---- BAKER SATURATION WEIGHTED
      DEN = (SW - SWCON) + SG
      IF (DEN .LE. SMALL) THEN
         KROIL = KOW
      ELSE
         KROIL = ((SW - SWCON)*KOW + SG*KOG) / DEN
      END IF
C
  490 IF (KROIL .LT. ZERO ) KROIL = ZERO
      IF (KROIL .GT. KROCW) KROIL = KROCW
      RETURN
      END
C
C-----------------------------------------------------------------------
      DOUBLE PRECISION FUNCTION KROGF (SG)
C-----------------------------------------------------------------------
C     OIL RELATIVE PERMEABILITY IN THE GAS-OIL SYSTEM.
C-----------------------------------------------------------------------
      INCLUDE 'PETRO.INC'
      COMMON /KREND/ SWCON, SORW, SORG, SGCON, EXPW, EXPO, EXPG,
     &               KRWMAX, KROMAX, KRGMAX, PCWMAX, PCGMAX
      DOUBLE PRECISION KRWMAX, KROMAX, KRGMAX
C
      DEN = ONE - SWCON - SGCON - SORG
      IF (DEN .LE. SMALL) THEN
         KROGF = ZERO
         RETURN
      END IF
      SN = (ONE - SWCON - SG - SORG) / DEN
      IF (SN .LE. ZERO) THEN
         KROGF = ZERO
      ELSE IF (SN .GE. ONE) THEN
         KROGF = KROMAX
      ELSE
         KROGF = KROMAX * SN**EXPO
      END IF
      RETURN
      END
C
C-----------------------------------------------------------------------
      DOUBLE PRECISION FUNCTION PCOW (SW)
C-----------------------------------------------------------------------
C     OIL-WATER CAPILLARY PRESSURE, PSI, BROOKS-COREY J FUNCTION FORM.
C-----------------------------------------------------------------------
      INCLUDE 'PETRO.INC'
      COMMON /KREND/ SWCON, SORW, SORG, SGCON, EXPW, EXPO, EXPG,
     &               KRWMAX, KROMAX, KRGMAX, PCWMAX, PCGMAX
      COMMON /KRTAB/ TSW(NTABMX), TKRW(NTABMX), TKRO(NTABMX),
     &               TPCW(NTABMX),
     &               TSG(NTABMX), TKRG(NTABMX), TKROG(NTABMX),
     &               TPCG(NTABMX)
      COMMON /KRSEL/ ITABLE, NSWOF, NSGOF, ISTONE
      DOUBLE PRECISION KRWMAX, KROMAX, KRGMAX
C
      IF (ITABLE .EQ. 1) THEN
         PCOW = TLOOK(TSW, TPCW, NSWOF, SW)
         RETURN
      END IF
C
      DEN = ONE - SWCON - SORW
      IF (DEN .LE. SMALL) THEN
         PCOW = ZERO
         RETURN
      END IF
      SN = (SW - SWCON) / DEN
      IF (SN .LT. 0.001D0) SN = 0.001D0
      IF (SN .GT. ONE)     SN = ONE
      PCOW = PCWMAX * SN**(-ONE/2.0D0) - PCWMAX
      IF (PCOW .GT. PCWMAX) PCOW = PCWMAX
      RETURN
      END
C
C-----------------------------------------------------------------------
      DOUBLE PRECISION FUNCTION TLOOK (X, Y, N, XX)
C-----------------------------------------------------------------------
C     PIECEWISE LINEAR LOOKUP WITH FLAT EXTRAPOLATION.  BINARY SEARCH.
C-----------------------------------------------------------------------
      INCLUDE 'PETRO.INC'
      DIMENSION X(N), Y(N)
C
      IF (N .LE. 0) THEN
         TLOOK = ZERO
         RETURN
      END IF
      IF (N .EQ. 1 .OR. XX .LE. X(1)) THEN
         TLOOK = Y(1)
         RETURN
      END IF
      IF (XX .GE. X(N)) THEN
         TLOOK = Y(N)
         RETURN
      END IF
C
      ILO = 1
      IHI = N
  500 IF (IHI - ILO .LE. 1) GO TO 510
         IMD = (ILO + IHI) / 2
         IF (XX .LT. X(IMD)) THEN
            IHI = IMD
         ELSE
            ILO = IMD
         END IF
         GO TO 500
C
  510 DX = X(IHI) - X(ILO)
      IF (DABS(DX) .LT. SMALL) THEN
         TLOOK = Y(ILO)
      ELSE
         TLOOK = Y(ILO) + (Y(IHI) - Y(ILO)) * (XX - X(ILO)) / DX
      END IF
      RETURN
      END
